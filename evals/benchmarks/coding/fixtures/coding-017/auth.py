import hashlib

def make_token(pw):
    return hashlib.md5(pw.encode()).hexdigest()

def check_token(pw, token):
    return hashlib.sha256(pw.encode()).hexdigest() == token
