import os
import json
from functools import wraps
from flask import session, redirect, url_for, jsonify
import config

# ==============================================================================
# RBAC: ROLE -> PERMISSIONS MAPPING
# ==============================================================================
ROLE_PERMISSIONS = {
    "admin": [
        "can_control_gate", 
        "can_repair", 
        "can_generate_reports", 
        "can_reset_penalties"
    ],
    "operator": [
        "can_control_gate"  # Operator can only control gates, but cannot repair or reset penalties
    ]
}

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
    with open(config.USERS_FILE, 'w') as f:
        json.dump(users, f, indent=4)

def has_permission(permission_name):
    """check if the current user has a specific permission based on their role"""
    role = session.get("role")
    if not role: 
        return False
    return permission_name in ROLE_PERMISSIONS.get(role, [])

def login_required(f):
    @wraps(f)
    def decorated_function(*args, **kwargs):
        if "username" not in session:
            return redirect(url_for("login"))
        return f(*args, **kwargs)
    return decorated_function

def admin_required(f):
    @wraps(f)
    def decorated_function(*args, **kwargs):
        if "username" not in session or session.get("role") != "admin":
            return "Access Denied: Admins Only", 403
        return f(*args, **kwargs)
    return decorated_function

def permission_required(permission_name):
    """RBAC 核心装饰器：拦截没有具体权限的 API 请求"""
    def decorator(f):
        @wraps(f)
        def decorated_function(*args, **kwargs):
            if "username" not in session:
                return jsonify({"status": "error", "message": "Unauthorized"}), 401
            if not has_permission(permission_name):
                return jsonify({"status": "error", "message": f"Forbidden: Requires '{permission_name}' permission."}), 403
            return f(*args, **kwargs)
        
        return decorated_function  
        
    return decorator