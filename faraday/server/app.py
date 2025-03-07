# Faraday Penetration Test IDE
# Copyright (C) 2016  Infobyte LLC (http://www.infobytesec.com/)
# See the file 'doc/LICENSE' for the license information
import logging
import os
import string
import datetime
import bleach
import pyotp
import requests
from flask_limiter import Limiter
from flask_limiter.util import get_remote_address

import jwt
from random import SystemRandom

from faraday.settings import load_settings
from faraday.server.config import LOCAL_CONFIG_FILE, copy_default_config_to_local
from faraday.server.extensions import socketio
from faraday.server.models import User, Role
from configparser import ConfigParser, NoSectionError, NoOptionError, DuplicateSectionError

import flask
import flask_login
from flask import Flask, session, g, request
from flask.json import JSONEncoder
from flask_sqlalchemy import get_debug_queries
from flask_security import (
    Security,
    SQLAlchemyUserDatastore,
)
from flask_security.forms import LoginForm
from flask_security.utils import (
    _datastore,
    get_message,
    verify_and_update_password,
    verify_hash)

from flask_kvsession import KVSessionExtension
from simplekv.fs import FilesystemStore
from simplekv.decorator import PrefixDecorator
from flask_login import user_logged_out, user_logged_in
from nplusone.ext.flask_sqlalchemy import NPlusOne
from depot.manager import DepotManager

import faraday.server.config
# Load SQLAlchemy Events
import faraday.server.events
from faraday.server.utils.logger import LOGGING_HANDLERS
from faraday.server.utils.invalid_chars import remove_null_caracters
from faraday.server.config import CONST_FARADAY_HOME_PATH

logger = logging.getLogger(__name__)
audit_logger = logging.getLogger('audit')


def setup_storage_path():
    default_path = CONST_FARADAY_HOME_PATH / 'storage'
    if not default_path.exists():
        logger.info(f'Creating directory {default_path}')
        default_path.mkdir()
    config = ConfigParser()
    config.read(faraday.server.config.LOCAL_CONFIG_FILE)
    try:
        config.add_section('storage')
        config.set('storage', 'path', str(default_path))
    except DuplicateSectionError:
        logger.info('Duplicate section storage. skipping.')
    with faraday.server.config.LOCAL_CONFIG_FILE.open('w') as configfile:
        config.write(configfile)

    return default_path


def register_blueprints(app):
    from faraday.server.api.modules.info import info_api  # pylint:disable=import-outside-toplevel
    from faraday.server.api.modules.commandsrun import commandsrun_api  # pylint:disable=import-outside-toplevel
    from faraday.server.api.modules.activity_feed import activityfeed_api  # pylint:disable=import-outside-toplevel
    from faraday.server.api.modules.credentials import credentials_api  # pylint:disable=import-outside-toplevel
    from faraday.server.api.modules.hosts import host_api  # pylint:disable=import-outside-toplevel
    from faraday.server.api.modules.licenses import license_api  # pylint:disable=import-outside-toplevel
    from faraday.server.api.modules.services import services_api  # pylint:disable=import-outside-toplevel
    from faraday.server.api.modules.session import session_api  # pylint:disable=import-outside-toplevel
    from faraday.server.api.modules.vulns import vulns_api  # pylint:disable=import-outside-toplevel
    from faraday.server.api.modules.vulnerability_template import \
        vulnerability_template_api  # pylint:disable=import-outside-toplevel
    from faraday.server.api.modules.workspaces import workspace_api  # pylint:disable=import-outside-toplevel
    from faraday.server.api.modules.handlers import handlers_api  # pylint:disable=import-outside-toplevel
    from faraday.server.api.modules.comments import comment_api  # pylint:disable=import-outside-toplevel
    from faraday.server.api.modules.upload_reports import upload_api  # pylint:disable=import-outside-toplevel
    from faraday.server.api.modules.websocket_auth import websocket_auth_api  # pylint:disable=import-outside-toplevel
    from faraday.server.api.modules.get_exploits import exploits_api  # pylint:disable=import-outside-toplevel
    from faraday.server.api.modules.custom_fields import \
        custom_fields_schema_api  # pylint:disable=import-outside-toplevel
    from faraday.server.api.modules.agent_auth_token import \
        agent_auth_token_api  # pylint:disable=import-outside-toplevel
    from faraday.server.api.modules.agent import agent_api  # pylint:disable=import-outside-toplevel
    from faraday.server.api.modules.bulk_create import bulk_create_api  # pylint:disable=import-outside-toplevel
    from faraday.server.api.modules.token import token_api  # pylint:disable=import-outside-toplevel
    from faraday.server.api.modules.search_filter import searchfilter_api  # pylint:disable=import-outside-toplevel
    from faraday.server.api.modules.preferences import preferences_api  # pylint:disable=import-outside-toplevel
    from faraday.server.api.modules.export_data import export_data_api  # pylint:disable=import-outside-toplevel
    from faraday.server.websockets import websockets  # pylint:disable=import-outside-toplevel
    from faraday.server.api.modules.settings_reports import \
        reports_settings_api  # pylint:disable=import-outside-toplevel
    from faraday.server.api.modules.settings_dashboard import \
        dashboard_settings_api  # pylint:disable=import-outside-toplevel

    app.register_blueprint(commandsrun_api)
    app.register_blueprint(activityfeed_api)
    app.register_blueprint(credentials_api)
    app.register_blueprint(host_api)
    app.register_blueprint(info_api)
    app.register_blueprint(license_api)
    app.register_blueprint(services_api)
    app.register_blueprint(session_api)
    app.register_blueprint(vulns_api)
    app.register_blueprint(vulnerability_template_api)
    app.register_blueprint(workspace_api)
    app.register_blueprint(handlers_api)
    app.register_blueprint(comment_api)
    app.register_blueprint(upload_api)
    app.register_blueprint(websocket_auth_api)
    app.register_blueprint(websockets)

    app.register_blueprint(exploits_api)
    app.register_blueprint(custom_fields_schema_api)
    app.register_blueprint(agent_api)
    app.register_blueprint(agent_auth_token_api)
    app.register_blueprint(bulk_create_api)
    app.register_blueprint(token_api)
    app.register_blueprint(searchfilter_api)
    app.register_blueprint(preferences_api)
    app.register_blueprint(export_data_api)
    app.register_blueprint(reports_settings_api)
    app.register_blueprint(dashboard_settings_api)


def check_testing_configuration(testing, app):
    if testing:
        app.config['SQLALCHEMY_ECHO'] = False
        app.config['TESTING'] = testing
        app.config['NPLUSONE_LOGGER'] = logging.getLogger('faraday.nplusone')
        app.config['NPLUSONE_LOG_LEVEL'] = logging.ERROR
        app.config['NPLUSONE_RAISE'] = True
        NPlusOne(app)


def register_handlers(app):
    # We are exposing a RESTful API, so don't redirect a user to a login page in
    # case of being unauthorized, raise a 403 error instead
    @app.login_manager.unauthorized_handler
    def unauthorized():  # pylint:disable=unused-variable
        flask.abort(403)

    def verify_token(token):
        try:
            data = jwt.decode(token, app.config['SECRET_KEY'], algorithms=["HS512"])
            user_id = data["user_id"]
            user = User.query.filter_by(fs_uniquifier=user_id).first()
            if not user or not verify_hash(data['validation_check'], user.password):
                logger.warn('Invalid authentication token. token invalid after password change')
                return None
            return user
        except jwt.ExpiredSignatureError:
            return None  # valid token, but expired
        except jwt.InvalidSignatureError:
            return None  # invalid token

    @app.login_manager.request_loader
    def load_user_from_request(request):
        if app.config['SECURITY_TOKEN_AUTHENTICATION_HEADER'] in flask.request.headers:
            header = flask.request.headers[app.config['SECURITY_TOKEN_AUTHENTICATION_HEADER']]
            try:
                (auth_type, token) = header.split(None, 1)
            except ValueError:
                logger.warn("Authorization header does not have type")
                flask.abort(401)
            auth_type = auth_type.lower()
            if auth_type == 'token':
                user = verify_token(token)
                if not user:
                    logger.warn('Invalid authentication token.')
                    flask.abort(401)
                else:
                    return user
            elif auth_type == 'agent':
                # Don't handle the agent logic here, do it in another
                # before_request handler
                return None
            elif auth_type == "basic":
                username = flask.request.authorization.get('username', '')
                password = flask.request.authorization.get('password', '')
                user = User.query.filter_by(username=username).first()
                if user and user.verify_and_update_password(password):
                    return user
            else:
                logger.warn("Invalid authorization type")
                flask.abort(401)

        # finally, return None if both methods did not login the user
        return None

    @app.before_request
    def default_login_required():  # pylint:disable=unused-variable
        view = app.view_functions.get(flask.request.endpoint)

        if flask_login.current_user.is_anonymous and not getattr(view, 'is_public', False) \
                and flask.request.method != 'OPTIONS':
            flask.abort(401)

    @app.before_request
    def load_g_custom_fields():  # pylint:disable=unused-variable
        g.custom_fields = {}

    @app.after_request
    def log_queries_count(response):  # pylint:disable=unused-variable
        if flask.request.method not in ['GET', 'HEAD']:
            # We did most optimizations for read only endpoints
            # TODO migrations: improve optimization and remove this if
            return response
        queries = get_debug_queries()
        max_query_time = max([q.duration for q in queries] or [0])
        if len(queries) > 15:
            logger.warn("Too many queries done (%s) in endpoint %s. "
                        "Maximum query time: %.2f",
                        len(queries), flask.request.endpoint, max_query_time)
            # from collections import Counter
            # print '\n\n\n'.join(
            #     map(str,Counter(q.statement for q in queries).most_common()))
        return response


def save_new_secret_key(app):
    if not LOCAL_CONFIG_FILE.exists():
        copy_default_config_to_local()
    config = ConfigParser()
    config.read(LOCAL_CONFIG_FILE)
    rng = SystemRandom()
    secret_key = "".join([rng.choice(string.ascii_letters + string.digits) for _ in range(25)])
    app.config['SECRET_KEY'] = secret_key
    try:
        config.set('faraday_server', 'secret_key', secret_key)
    except NoSectionError:
        config.add_section('faraday_server')
        config.set('faraday_server', 'secret_key', secret_key)
    with open(LOCAL_CONFIG_FILE, 'w') as configfile:
        config.write(configfile)


def save_new_agent_creation_token_secret():
    assert LOCAL_CONFIG_FILE.exists()
    config = ConfigParser()
    config.read(LOCAL_CONFIG_FILE)
    registration_secret = pyotp.random_base32()
    config.set('faraday_server', 'agent_registration_secret', registration_secret)
    with open(LOCAL_CONFIG_FILE, 'w') as configfile:
        config.write(configfile)
    faraday.server.config.faraday_server.agent_registration_secret = registration_secret


def expire_session(app, user):
    logger.debug("Cleanup sessions")
    session.destroy()
    KVSessionExtension(app=app).cleanup_sessions(app)

    user_ip = request.headers.get('X-Forwarded-For', request.remote_addr)
    user_logout_at = datetime.datetime.utcnow()
    audit_logger.info(f"User [{user.username}] logged out from IP [{user_ip}] at [{user_logout_at}]")
    logger.info(f"User [{user.username}] logged out from IP [{user_ip}] at [{user_logout_at}]")


def user_logged_in_succesfull(app, user):
    user_agent = request.headers.get('User-Agent')
    if user_agent.startswith('faraday-client/'):
        HOME_URL = "https://portal.faradaysec.com/api/v1/license_check"
        params = {'version': faraday.__version__, 'key': 'white', 'client': user_agent}
        try:
            logger.debug('Send Faraday-Client license_check')
            res = requests.get(HOME_URL, params=params, timeout=1, verify=True)
            logger.debug("Faraday-Client license_check response: %s", res.text)
        except Exception as e:
            logger.warning("Error sending client license_check [%s]", e)
    # cleanup old sessions
    logger.debug("Cleanup sessions")
    KVSessionExtension(app=app).cleanup_sessions(app)

    user_ip = request.headers.get('X-Forwarded-For', request.remote_addr)
    user_login_at = datetime.datetime.utcnow()
    audit_logger.info(f"User [{user.username}] logged in from IP [{user_ip}] at [{user_login_at}]")
    logger.info(f"User [{user.username}] logged in from IP [{user_ip}] at [{user_login_at}]")


def uia_username_mapper(identity):
    return bleach.clean(identity, strip=True)


def create_app(db_connection_string=None, testing=None):
    class CustomFlask(Flask):
        SKIP_RULES = [  # These endpoints will be removed for v3
            '/v3/ws/<workspace_name>/hosts/bulk_delete/',
            '/v3/ws/<workspace_name>/vulns/bulk_delete/',
            '/v3/ws/<workspace_id>/change_readonly/',
            '/v3/ws/<workspace_id>/deactivate/',
            '/v3/ws/<workspace_id>/activate/',
        ]

        def add_url_rule(self, rule, endpoint=None, view_func=None, **options):
            # Flask registers views when an application starts
            # do not add view from SKIP_VIEWS
            for rule_ in CustomFlask.SKIP_RULES:
                if rule_ == rule:
                    return
            return super().add_url_rule(rule, endpoint, view_func, **options)

    app = CustomFlask(__name__, static_folder=None)

    try:
        secret_key = faraday.server.config.faraday_server.secret_key
    except Exception:
        # Now when the config file does not exist it doesn't enter in this
        # condition, but it could happen in the future. TODO check
        save_new_secret_key(app)
    else:
        if secret_key is None:
            # This is what happens now when the config file doesn't exist.
            # TODO check
            save_new_secret_key(app)
        else:
            app.config['SECRET_KEY'] = secret_key

    if faraday.server.config.faraday_server.agent_registration_secret is None:
        save_new_agent_creation_token_secret()

    login_failed_message = ("Invalid username or password", 'error')

    app.config.update({
        'SECURITY_BACKWARDS_COMPAT_AUTH_TOKEN': True,
        'SECURITY_PASSWORD_SINGLE_HASH': True,
        'WTF_CSRF_ENABLED': False,
        'SECURITY_USER_IDENTITY_ATTRIBUTES': [{'username': {'mapper': uia_username_mapper}}],
        'SECURITY_POST_LOGIN_VIEW': '/_api/session',
        'SECURITY_POST_CHANGE_VIEW': '/_api/change',
        'SECURITY_RESET_PASSWORD_TEMPLATE': '/security/reset.html',
        'SECURITY_POST_RESET_VIEW': '/',
        'SECURITY_SEND_PASSWORD_RESET_EMAIL': True,
        # For testing porpouse
        'SECURITY_EMAIL_SENDER': "noreply@infobytesec.com",
        'SECURITY_CHANGEABLE': True,
        'SECURITY_SEND_PASSWORD_CHANGE_EMAIL': False,
        'SECURITY_MSG_USER_DOES_NOT_EXIST': login_failed_message,
        'SECURITY_TOKEN_AUTHENTICATION_HEADER': 'Authorization',

        # The line bellow should not be necessary because of the
        # CustomLoginForm, but i'll include it anyway.
        'SECURITY_MSG_INVALID_PASSWORD': login_failed_message,

        'SESSION_TYPE': 'filesystem',
        'SESSION_FILE_DIR': faraday.server.config.FARADAY_SERVER_SESSIONS_DIR,

        'SQLALCHEMY_TRACK_MODIFICATIONS': False,
        'SQLALCHEMY_RECORD_QUERIES': True,
        # app.config['SQLALCHEMY_ECHO'] = True
        'SECURITY_PASSWORD_SCHEMES': [
            'bcrypt',  # This should be the default value
            # 'des_crypt',
            # 'pbkdf2_sha256',
            # 'pbkdf2_sha512',
            # 'sha256_crypt',
            # 'sha512_crypt',
        ],
        'PERMANENT_SESSION_LIFETIME': datetime.timedelta(
            hours=int(faraday.server.config.faraday_server.session_timeout or 12)),
        'SESSION_COOKIE_NAME': 'faraday_session_2',
        'SESSION_COOKIE_SAMESITE': 'Lax',
    })

    store = FilesystemStore(app.config['SESSION_FILE_DIR'])
    prefixed_store = PrefixDecorator('sessions_', store)
    KVSessionExtension(prefixed_store, app)
    user_logged_in.connect(user_logged_in_succesfull, app)
    user_logged_out.connect(expire_session, app)

    storage_path = faraday.server.config.storage.path
    if not storage_path:
        logger.warn(
            'No storage section or path in the .faraday/config/server.ini. Setting the default value to .faraday/storage')
        storage_path = setup_storage_path()

    if not DepotManager.get('default'):
        if testing:
            DepotManager.configure('default', {
                'depot.storage_path': '/tmp'  # nosec
            })
        else:
            DepotManager.configure('default', {
                'depot.storage_path': storage_path
            })
    app.config['SQLALCHEMY_ECHO'] = 'FARADAY_LOG_QUERY' in os.environ
    check_testing_configuration(testing, app)

    try:
        app.config[
            'SQLALCHEMY_DATABASE_URI'] = db_connection_string or faraday.server.config.database.connection_string.strip(
            "'")
    except AttributeError:
        logger.info(
            'Missing [database] section on server.ini. Please configure the database before running the server.')
    except NoOptionError:
        logger.info(
            'Missing connection_string on [database] section on server.ini. Please configure the database before running the server.')

    from faraday.server.models import db  # pylint:disable=import-outside-toplevel
    db.init_app(app)
    # Session(app)

    # Setup Flask-Security
    app.user_datastore = SQLAlchemyUserDatastore(
        db,
        user_model=User,
        role_model=Role)

    from faraday.server.api.modules.agent import agent_creation_api  # pylint: disable=import-outside-toplevel

    app.limiter = Limiter(
        app,
        key_func=get_remote_address,
        default_limits=[]
    )
    if not testing:
        app.limiter.limit(faraday.server.config.limiter_config.login_limit)(agent_creation_api)

    app.register_blueprint(agent_creation_api)

    Security(app, app.user_datastore, login_form=CustomLoginForm)
    # Make API endpoints require a login user by default. Based on
    # https://stackoverflow.com/questions/13428708/best-way-to-make-flask-logins-login-required-the-default

    app.view_functions['security.login'].is_public = True
    app.view_functions['security.logout'].is_public = True
    app.debug = faraday.server.config.is_debug_mode()
    minify_json_output(app)

    for handler in LOGGING_HANDLERS:
        app.logger.addHandler(handler)
    app.logger.propagate = False
    register_blueprints(app)
    register_handlers(app)

    app.view_functions['agent_creation_api.AgentCreationView:post'].is_public = True

    register_extensions(app)
    load_settings()

    return app


def register_extensions(app):
    socketio.init_app(app)


def minify_json_output(app):
    class MiniJSONEncoder(JSONEncoder):
        item_separator = ','
        key_separator = ':'

    app.json_encoder = MiniJSONEncoder
    app.config['JSONIFY_PRETTYPRINT_REGULAR'] = False


class CustomLoginForm(LoginForm):
    """A login form that does shows the same error when the username
    or the password is invalid.

    The builtin form of flask_security generates different messages
    so it is possible for an attacker to enumerate usernames
    """

    def validate(self):

        user_ip = request.headers.get('X-Forwarded-For', request.remote_addr)
        time_now = datetime.datetime.utcnow()

        # Use super of LoginForm, not super of CustomLoginForm, since I
        # want to skip the LoginForm validate logic
        if not super(LoginForm, self).validate():
            audit_logger.warning(f"Invalid Login - User [{self.email.data}] from IP [{user_ip}] at [{time_now}]")
            logger.warning(f"Invalid Login - User [{self.email.data}] from IP [{user_ip}] at [{time_now}]")
            return False
        self.email.data = remove_null_caracters(self.email.data)

        self.user = _datastore.find_user(username=self.email.data)

        if self.user is None:
            audit_logger.warning(f"Invalid Login - User [{self.email.data}] from IP [{user_ip}] at [{time_now}] - "
                                 f"Reason: [Invalid Username]")
            logger.warning(f"Invalid Login - User [{self.email.data}] from IP [{user_ip}] at [{time_now}] - "
                           f"Reason: [Invalid Username]")
            self.email.errors.append(get_message('USER_DOES_NOT_EXIST')[0])
            return False

        self.user.password = remove_null_caracters(self.user.password)
        if not self.user.password:
            audit_logger.warning(f"Invalid Login - User [{self.email.data}] from IP [{user_ip}] at [{time_now}] - "
                                 f"Reason: [Invalid Password]")
            logger.warning(f"Invalid Login - User [{self.email.data}] from IP [{user_ip}] at [{time_now}] - "
                           f"Reason: [Invalid Password]")
            self.email.errors.append(get_message('USER_DOES_NOT_EXIST')[0])
            return False
        self.password.data = remove_null_caracters(self.password.data)
        if not verify_and_update_password(self.password.data, self.user):
            audit_logger.warning(f"Invalid Login - User [{self.email.data}] from IP [{user_ip}] at [{time_now}] - "
                                 f"Reason: [Invalid Password]")
            logger.warning(f"Invalid Login - User [{self.email.data}] from IP [{user_ip}] at [{time_now}] - "
                           f"Reason: [Invalid Password]")
            self.email.errors.append(get_message('USER_DOES_NOT_EXIST')[0])
            return False
        # if requires_confirmation(self.user):
        #     self.email.errors.append(get_message('CONFIRMATION_REQUIRED')[0])
        #     return False
        if not self.user.is_active:
            audit_logger.warning(f"Invalid Login - User [{self.email.data}] from IP [{user_ip}] at [{time_now}] - "
                                 f"Reason: [Disabled Account]")
            logger.warning(f"Invalid Login - User [{self.email.data}] from IP [{user_ip}] at [{time_now}] - "
                           f"Reason: [Disabled Account]")
            self.email.errors.append(get_message('DISABLED_ACCOUNT')[0])
            return False
        return True
