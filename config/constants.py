import os
import dotenv

dotenv.load() or dotenv.load('.env')

DEV_EMAIL = dotenv.get('DEV_EMAIL', default=None)

DEBUG = dotenv.get('DEBUG', default=False)

HOST = dotenv.get('HOST')
HTTPS = dotenv.get('HTTPS', default=True)

PROJECTPATH = dotenv.get('PROJECTPATH')

FLASK_SECRET_KEY = dotenv.get('FLASK_SECRET_KEY')

APP_LOG_FILENAME = os.path.join(PROJECTPATH, 'app.log')

SQLALCHEMY_DATABASE_URI = dotenv.get('DATABASE_URL')

SENDGRID_API_KEY = dotenv.get('SENDGRID_API_KEY')

TEMPLATE_ROOT = os.path.join(PROJECTPATH, 'templates')
STATIC_ROOT = os.path.join(PROJECTPATH, 'static')