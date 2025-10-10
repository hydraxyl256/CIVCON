import ssl, socket

host = "api.sandbox.africastalking.com"
port = 443
ctx = ssl.create_default_context()

print(f"Testing TLS to {host}:{port}")
with socket.create_connection((host, port)) as sock:
    with ctx.wrap_socket(sock, server_hostname=host) as ssock:
        print("✅ Connected with:", ssock.version())
