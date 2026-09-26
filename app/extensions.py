from flask_sqlalchemy import SQLAlchemy
from flask_login import LoginManager
from flask_migrate import Migrate

# session_options passed to the constructor directly — Flask-SQLAlchemy
# 3.x does NOT read a SQLALCHEMY_SESSION_OPTIONS config key (confirmed by
# testing in an earlier build of this app; setting it in config.py was
# silently ignored). expire_on_commit=False stops a committed object's
# attributes from silently triggering a fresh SELECT the next time
# they're touched — that fresh SELECT runs in a brand-new transaction,
# which needs tenant context re-applied, and a *silent* extra query is a
# much easier place to forget that than an explicit one.
db = SQLAlchemy(session_options={"expire_on_commit": False})
login_manager = LoginManager()
migrate = Migrate()
