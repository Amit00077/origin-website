import collections
from datetime import datetime, timedelta
import json
import math
import re
import sys
import time
import os

from backoff import on_exception, expo
from config import constants
from database import db, db_models, db_common
from ratelimit import limits, sleep_and_retry, RateLimitException
import requests
from tools import db_utils
from util import sendgrid_wrapper as sgw
from util import time_

from redis import Redis

import json

redis_client = Redis.from_url(os.environ['REDIS_URL'])

# NOTE: remember to use lowercase addresses for everything

# token contract addresses
ogn_contract = "0x8207c1ffc5b6804f6024322ccf34f29c3541ae26"
dai_contract = "0x89d24a6b4ccb1b6faa2625fe562bdd9a23260359"

# ogn wallet addresses
foundation_reserve_address = "0xe011fa2a6df98c69383457d87a056ed0103aa352"
team_dist_address = "0xcaa5ef7abc36d5e5a3e4d7930dcff3226617a167"
investor_dist_address = "0x3da5045699802ea1fcc60130dedea67139c5b8c0"
dist_staging_address = "0x1a34e5b97d684b124e32bd3b7dc82736c216976b"
partnerships_address = "0xbc0722eb6e8ba0217aeea5694fe4f214d2e53017"
ecosystem_growth_address = "0x2d00c3c132a0567bbbb45ffcfd8c6543e08ff626"

# start tracking a wallet address
def add_contact(address, **kwargs):

    # nothing to do here, bail
    if not address:
        return False

    address = address.strip()

    # must look like an ETH address
    if not re.match("^(0x)?[0-9a-fA-F]{40}$", address):
        return False

    contact = db_common.get_or_create(
        db.session, db_models.EthContact, address=address.lower()
    )

    allowed_fields = [
        "name",
        "email",
        "phone",
        "investor",
        "presale_interest",
        "investor_airdrop",
        "dapp_user",
        "employee",
        "exchange",
        "company_wallet",
        "desc",
        "country_code",
    ]
    for key, value in kwargs.items():
        if key in allowed_fields:
            if value:
                # Normalize email to lower case before storing in the DB.
                if key == "email":
                    value = value.lower()
                setattr(contact, key, value)
        else:
            raise Exception("Unknown field")

    db.session.add(contact)
    db.session.commit()


def lookup_details(address):
    # automatically start tracking every wallet that receives OGN
    contact = db_common.get_or_create(
        db.session, db_models.EthContact, address=address.lower()
    )
    db.session.add(contact)
    db.session.commit()
    return contact


# limit calls to 5 requests / second per their limits
# https://etherscan.io/apis
@sleep_and_retry
@limits(calls=5, period=1)
def call_etherscan(url):
    raw_json = requests.get(url)
    return raw_json.json()


# limit calls to 3 requests / second per their limits
# https://amberdata.io/pricing
@sleep_and_retry
@limits(calls=3, period=1)
def call_amberdata(url):
    headers = {"x-api-key": constants.AMBERDATA_KEY}
    raw_json = requests.get(url, headers=headers)
    return raw_json.json()


# limit calls to 10 requests / second per their limits
# https://github.com/EverexIO/Ethplorer/wiki/Ethplorer-API#api-keys-limits
@sleep_and_retry
@limits(calls=10, period=1)
def call_ethplorer(url):
    url = "%s?apiKey=%s" % (url, constants.ETHPLORER_KEY)
    raw_json = requests.get(url)
    return raw_json.json()


# this script is called on a 10 minute cron by Heroku
# break things up so we update slowly throughout the day instead of in one big batch
def get_some_contacts():
    per_run = 24 * 6  # every ten minutes
    total = db.session.query(db_models.EthContact.address).count()
    limit = int(total / per_run) + 1
    print "checking %d wallets" % (limit)
    EC = db_models.EthContact
    return (
        EC.query.filter(EC.last_updated < time_.days_before_now(1))
        .order_by(EC.last_updated)
        .limit(limit)
        .all()
    )


# track the holdings of every wallet that we're watching
def fetch_eth_balances_from_etherscan():

    # etherscan allows us to query the ETH balance of 20 addresses at a time
    chunk = 20

    contacts = get_some_contacts()

    groups = [
        contacts[i * chunk : (i + 1) * chunk]
        for i in range((len(contacts) + chunk - 1) // chunk)
    ]
    for group in groups:
        address_list = ",".join([str(x.address) for x in group])

        url = (
            "https://api.etherscan.io/api?module=account&action=balancemulti&address=%s&tag=latest&apikey=%s"
            % (address_list, constants.ETHERSCAN_KEY)
        )
        results = call_etherscan(url)

        try:
            # loop through every wallet we're tracking and update the ETH balance
            for result in results["result"]:
                print "Fetching ETH balance for %s" % (result["account"])
                wallet = db_models.EthContact.query.filter_by(
                    address=result["account"].lower()
                ).first()
                # intentionally using ETH instead of WEI to be more human-friendly, despite being less precise
                if result["balance"]:
                    wallet.eth_balance = float(result["balance"]) / math.pow(10, 18)
                else:
                    print "invalid address: %s" % (result["account"])
                wallet.last_updated = datetime.utcnow()
                db.session.add(wallet)
                db.session.commit()
        except Exception as e:
            print e
            print results


# amberdata seems to have the fastest API
def fetch_tokens_from_amberdata():

    contacts = get_some_contacts()

    for contact in contacts:
        print "Fetching token balances for %s" % (contact.address)

        contact.tokens = []

        per_page = 100
        page = 0

        keep_looking = True

        # pagination
        while keep_looking:
            try:

                url = (
                    "https://web3api.io/api/v1/addresses/%s/tokens?page=%d&size=%d"
                    % (contact.address, page, per_page)
                )
                print url
                results = call_amberdata(url)

                # print results

                contact.tokens = contact.tokens + results["payload"]["records"]
                contact.token_count = results["payload"]["totalRecords"]

                print "%s tokens found. fetching page %s" % (contact.token_count, page)

                for token in results["payload"]["records"]:
                    if token["address"] == ogn_contract:
                        contact.ogn_balance = float(token["amount"]) / math.pow(10, 18)
                    elif token["address"] == dai_contract:
                        contact.dai_balance = float(token["amount"]) / math.pow(10, 18)

                if (
                    int(results["payload"]["totalRecords"]) <= per_page
                    or len(results["payload"]["records"]) < per_page
                ):
                    keep_looking = False
                    break
                else:
                    page = page + 1
            except Exception as e:
                print e
                time.sleep(1)
                print "retrying"

        contact.last_updated = datetime.utcnow()
        db.session.add(contact)
        db.session.commit()


# use ethplorer to fetch eth balance & token holdings
def fetch_from_ethplorer():

    contacts = get_some_contacts()

    for contact in contacts:

        print "Fetching tokens & ETH balance for %s" % (contact.address)

        try:

            url = "http://api.ethplorer.io/getAddressInfo/%s" % (contact.address)
            results = call_ethplorer(url)

            contact.eth_balance = results["ETH"]["balance"]
            contact.transaction_count = results["countTxs"]

            if "tokens" in results:
                contact.tokens = results["tokens"]
                # update the OGN & DAI balance
                for token in results["tokens"]:
                    if token["tokenInfo"]["address"].lower() == ogn_contract:
                        contact.ogn_balance = float(token["balance"]) / math.pow(10, 18)
                    elif token["tokenInfo"]["address"].lower() == dai_contract:
                        contact.dai_balance = float(token["balance"]) / math.pow(10, 18)
                contact.token_count = len(results["tokens"])
            contact.last_updated = datetime.utcnow()

            db.session.add(contact)
            db.session.commit()

        except Exception as e:
            print e
            time.sleep(1)
            print "retrying"


# monitor & alert on all movement of OGN
def fetch_ogn_transactions():

    etherscan_url = (
        "http://api.etherscan.io/api?module=account&action=tokentx&contractaddress=%s&startblock=0&endblock=999999999&sort=desc&apikey=%s"
        % (ogn_contract, constants.ETHERSCAN_KEY)
    )
    # print etherscan_url
    results = call_etherscan(etherscan_url)

    # loop through every transaction where Origin tokens were moved
    for result in results["result"]:
        tx = db_common.get_or_create(
            db.session, db_models.TokenTransaction, tx_hash=result["hash"]
        )
        tx.from_address = result["from"].lower()
        tx.to_address = result["to"].lower()
        # intentionally using ETH instead of WEI to be more human-friendly, despite being less precise
        tx.amount = float(result["value"]) / math.pow(10, 18)
        tx.block_number = result["blockNumber"]
        tx.timestamp = time_.fromtimestamp(result["timeStamp"])

        if tx.amount > 0:
            print "%g OGN moved in transaction %s" % (tx.amount, result["hash"])

        # send an email alert every time OGN tokens are moved
        # only alert once & ignore marketplace transactions which show up as 0 OGN
        if tx.amount > 1000 and not tx.notification_sent:
            to_details = lookup_details(tx.to_address)
            from_details = lookup_details(tx.from_address)

            if from_details.name and to_details.name:
                subject = "%s moved %g OGN to %s" % (
                    from_details.name,
                    tx.amount,
                    to_details.name,
                )
            elif from_details.name:
                subject = "%s moved %g OGN" % (from_details.name, tx.amount)
            elif to_details.name:
                subject = "%g OGN moved to %s" % (tx.amount, to_details.name)
            else:
                subject = "%g OGN moved" % (tx.amount)

            body = u"""
                {amount} OGN <a href='https://etherscan.io/tx/{tx_hash}'>moved</a>
                from <a href='https://etherscan.io/address/{from_address}'>{from_name}</a>
                to <a href='https://etherscan.io/address/{to_address}'>{to_name}</a>
            """.format(
                amount="{0:g}".format(float(tx.amount)),
                tx_hash=tx.tx_hash,
                from_name=from_details.name if from_details.name else tx.from_address,
                from_address=tx.from_address,
                to_name=to_details.name if to_details.name else tx.to_address,
                to_address=tx.to_address,
            )

            print subject

            sgw.notify_founders(body, subject)
            tx.notification_sent = True
            db.session.add(tx)
            db.session.commit()

# Fetches wallet balance from API and stores that to DB
def fetch_wallet_balance(wallet):
    print "Checking the balance of wallet %s" % (
        wallet,
    )

    url = "http://api.ethplorer.io/getAddressInfo/%s" % (wallet)
    results = call_ethplorer(url)

    contact = db_common.get_or_create(
        db.session, db_models.EthContact, address=wallet
    )

    if "error" in results:
        print("Error while fetching balance")
        print(results["error"]["message"])
        raise ValueError(results["error"]["message"])

    contact.eth_balance = results["ETH"]["balance"]
    contact.transaction_count = results["countTxs"]

    print "ETH balance of %s is %s" % (wallet, results["ETH"]["balance"])
    if "tokens" in results:
        contact.tokens = results["tokens"]
        # update the OGN & DAI balance
        for token in results["tokens"]:
            if token["tokenInfo"]["address"] == ogn_contract:
                contact.ogn_balance = float(token["balance"]) / math.pow(10, 18)
            elif token["tokenInfo"]["address"] == dai_contract:
                contact.dai_balance = float(token["balance"]) / math.pow(10, 18)
        contact.token_count = len(results["tokens"])
    contact.last_updated = datetime.utcnow()

    db.session.add(contact)
    db.session.commit()

    return contact

# alerting system to notify us if a wallet drops below a certain threshold
def alert_on_balance_drop(wallet, label, eth_threshold):

    print "Checking if the balance of %s (%s) is below %s" % (
        wallet,
        label,
        eth_threshold,
    )

    try:
        contact  = fetch_wallet_balance(wallet)

        print (contact.eth_balance)

        if contact.eth_balance < eth_threshold:
            print "Low balance. Notifying."
            subject = "%s purse is running low. %s ETH remaining" % (
                label,
                contact.eth_balance,
            )
            body = "Please send more ETH to %s" % (wallet)
            print (body)
            print (subject)
            sgw.notify_founders(body, subject)

    except Exception as e:
        print e

# Fetches and stores OGN & ETH prices froom CoinGecko
def fetch_token_prices():
    print("Fetching token prices...")
    
    try:
        url = "https://api.coingecko.com/api/v3/simple/price?ids=origin-protocol%2Cethereum&vs_currencies=usd"
        raw_json = requests.get(url)
        response = raw_json.json()

        if "error" in response:
            print("Error while fetching balance")
            print(response["error"]["message"])
            raise ValueError(response["error"]["message"])

        ogn_usd_price = response["origin-protocol"]["usd"]
        eth_usd_price = response["ethereum"]["usd"]

        redis_client.set("ogn_usd_price", ogn_usd_price)
        redis_client.set("eth_usd_price", eth_usd_price)

        print "Set OGN price to %s" % ogn_usd_price
        print "Set ETH price to %s" % eth_usd_price

    except Exception as e:
        print("Failed to load token prices")
        print e

def fetch_stats_from_t3(investor_portal = True):
    print("Fetching T3 stats...")

    url = "https://remote.team.originprotocol.com/api/user-stats"

    if investor_portal:
        url = "https://remote.investor.originprotocol.com/api/user-stats"

    raw_json = requests.get(url)
    response = raw_json.json()

    return response

def fetch_staking_stats():
    print("Fetching T3 user stats...")
    
    try:
        investor_stats = fetch_stats_from_t3(investor_portal=True)
        team_stats = fetch_stats_from_t3(investor_portal=False)

        investor_staked_users = int(investor_stats["userCount"] or 0)
        investor_locked_sum = int(investor_stats["lockupSum"] or 0)

        team_staked_users = int(team_stats["userCount"] or 0)
        team_locked_sum = int(team_stats["lockupSum"] or 0)

        sum_users = investor_staked_users + team_staked_users
        sum_tokens = investor_locked_sum + team_locked_sum

        redis_client.set("staked_user_count", sum_users)
        redis_client.set("staked_token_count", sum_tokens)

        print "There are %s T3 users and %s locked up tokens" % (sum_users, sum_tokens)

    except Exception as e:
        print("Failed to load T3 user stats")
        print e

# Fetches reserved wallet balances and token price 
# and recalculates things to be shown in
def compute_ogn_stats():
    print("Computing OGN stats...")
    # Fetch OGN and ETH prices
    fetch_token_prices()

    fetch_staking_stats()

    # Fetch reserved wallet balances
    fetch_wallet_balance(foundation_reserve_address)
    fetch_wallet_balance(team_dist_address)
    fetch_wallet_balance(investor_dist_address)
    fetch_wallet_balance(dist_staging_address)
    fetch_wallet_balance(partnerships_address)
    fetch_wallet_balance(ecosystem_growth_address)

    # Update circulating supply
    update_circulating_supply()

def get_wallet_balance_from_db(wallet):
    contact = db_models.EthContact.query.filter_by(address=wallet).first()

    if contact is None:
        return 0

    return contact.ogn_balance

def get_ogn_stats(format_data = True):
    total_supply = 1000000000

    ogn_usd_price = float(redis_client.get("ogn_usd_price") or 0)
    staked_user_count = int(redis_client.get("staked_user_count") or 0)
    staked_token_count = int(redis_client.get("staked_token_count") or 0)

    results = db_models.EthContact.query.filter(db_models.EthContact.address.in_((
        foundation_reserve_address,
        team_dist_address,
        investor_dist_address,
        dist_staging_address,
        partnerships_address,
        ecosystem_growth_address,
    ))).all()

    ogn_balances = dict([(result.address, result.ogn_balance) for result in results])

    foundation_reserve_balance = ogn_balances[foundation_reserve_address]
    team_dist_balance = ogn_balances[team_dist_address]
    investor_dist_balance = ogn_balances[investor_dist_address]
    dist_staging_balance = ogn_balances[dist_staging_address]
    partnerships_balance = ogn_balances[partnerships_address]
    ecosystem_growth_balance = ogn_balances[ecosystem_growth_address]

    reserved_tokens = int(
        foundation_reserve_balance +
        team_dist_balance +
        investor_dist_balance +
        dist_staging_balance +
        partnerships_balance +
        ecosystem_growth_balance
    )

    circulating_supply = int(total_supply - reserved_tokens)

    market_cap = int(circulating_supply * ogn_usd_price)

    out_data = dict([
        ("ogn_usd_price", ogn_usd_price),
        ("circulating_supply", circulating_supply),
        ("market_cap", market_cap),
        ("total_supply", total_supply),

        ("reserved_tokens", reserved_tokens),
        ("staked_user_count", staked_user_count),
        ("staked_token_count", staked_token_count),

        ("foundation_reserve_address", foundation_reserve_address),
        ("team_dist_address", team_dist_address),
        ("investor_dist_address", investor_dist_address),
        ("dist_staging_address", dist_staging_address),
        ("partnerships_address", partnerships_address),
        ("ecosystem_growth_address", ecosystem_growth_address),
    ])

    if format_data:
        out_data["ogn_usd_price"] = '${:,}'.format(ogn_usd_price)
        out_data["circulating_supply"] = '{:,}'.format(circulating_supply)
        out_data["market_cap"] = '{:,}'.format(market_cap)
        out_data["total_supply"] = '{:,}'.format(total_supply)
        out_data["reserved_tokens"] = '{:,}'.format(reserved_tokens)
        out_data["staked_user_count"] = '{:,}'.format(staked_user_count)
        out_data["staked_token_count"] = '{:,}'.format(staked_token_count)

    return out_data
    
def get_supply_history():
    data =  redis_client.get("ogn_supply_data") or "[]"

    return data

def update_circulating_supply():
    stats = get_ogn_stats(format_data=False)
    snapshot_date = datetime.utcnow()

    supply_snapshot = db_common.get_or_create(
        db.session, db_models.CirculatingSupply, snapshot_date=snapshot_date
    )

    supply_snapshot.supply_amount = stats["circulating_supply"]
    db.session.commit()

    supply_data = db.engine.execute("""
    select timewin, max(s.supply_amount)
    from 
        generate_series(now() - interval '12 month', now(), '1 day') as timewin
    left outer join 
        (select * from circulating_supply where snapshot_date > now() - interval '12 month' and snapshot_date > '2020-01-01'::date order by snapshot_date desc) s
    on s.snapshot_date < timewin 
        and s.snapshot_date >= timewin - (interval '1 day')
    where timewin > '2020-01-01'::date
    group by timewin
    order by timewin desc
    """)

    out = []

    supply_data_list = list(supply_data)
    latest_supply = supply_data_list[0][1]

    for row in supply_data_list:
        if row[1] is not None:
            latest_supply = row[1]

        out.append(dict([
            ("supply_amount", row[1] or latest_supply),
            ("snapshot_date", row[0].strftime("%Y/%m/%d %H:%M:%S"))
        ]))

    redis_client.set("ogn_supply_data", json.dumps(list(reversed(out))))

    print "Updated current circulating supply to %s" % stats["circulating_supply"]

if __name__ == "__main__":
    # called via cron on Heroku
    with db_utils.request_context():
        # fetch_ogn_transactions()
        alert_on_balance_drop("0x440EC5490c26c58A3c794f949345b10b7c83bdC2", "AC", 1)
        # alert_on_balance_drop("0x5fabfc823e13de8f1d138953255dd020e2b3ded0", "Meta-transactions", 1)
        # fetch_from_ethplorer()

        compute_ogn_stats()
        
