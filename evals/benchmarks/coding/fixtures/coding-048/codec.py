def encode(n):
    alphabet = 'abcdefghjkmnpqrstuvwxyz23456789'
    if n == 0:
        return alphabet[0]
    out = ''
    while n:
        n, r = divmod(n, 31)
        out = alphabet[r] + out
    return out
