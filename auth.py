import os
import json
from functools import wraps
from flask import session, redirect, url_for
import config

def load_users():
    if not os.path.exists(config.USERS_FILE):
        default_users = {
            "admin": {"password": "admin", "role": "admin"},
            "operator": {"password": "operator", "role": "operator"}
        }
        save_users(default_users)
        return default_users
    
    with open(config.USERS_FILE, 'r') as f:
        return json.load(f)

def save_users(users):
    """Save the users dictionary to the users.json file."""
    with open(config.USERS_FILE, 'w') as f:
        json.dump(users, f, indent=4)

def login_required(f):
    """check if the user is logged in"""
    @wraps(f)
    def decorated_function(*args, **kwargs):
        if "username" not in session:
            return redirect(url_for("login"))
        return f(*args, **kwargs)
    return decorated_function

def admin_required(f):
    """check if the user is an admin"""
    @wraps(f)
    def decorated_function(*args, **kwargs):
        if "username" not in session or session.get("role") != "admin":
            return "Access Denied: Admins Only", 403
        return f(*args, **kwargs)
    return decorated_function