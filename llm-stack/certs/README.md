# 🔐 Certificates (`certs/`)

This directory is used to store TLS certificates for the LLM stack.

⚠️ **Important:**
This folder is ignored by Git and must never contain real private keys committed to the repository.

---

# 📁 Expected Files

```id="x2v1sl"
certs/
├── cert.pem   # Public certificate
└── key.pem    # Private key
```

---

# 🧪 Option 1 — Self-Signed Certificate (Local Development)

Use this for local testing (`localhost`, LAN IP, etc.).

## Generate certificate

```bash id="kzv31f"
mkdir -p certs

openssl req -x509 -nodes -days 365 \
-newkey rsa:2048 \
-keyout certs/key.pem \
-out certs/cert.pem \
-subj "/CN=localhost"
```

---

## Result

* `cert.pem` → certificate
* `key.pem` → private key

---

## Notes

* Browsers will show a warning (expected)
* Suitable for:

    * local development
    * internal networks
    * testing HTTPS

---

# 🌐 Option 2 — Self-Signed with Subject Alternative Names (Recommended)

Modern clients require SAN (Subject Alternative Names).

```bash id="z8h9nt"
cat > certs/openssl.cnf << 'EOF'
[req]
default_bits       = 2048
distinguished_name = req_distinguished_name
req_extensions     = req_ext
prompt = no

[req_distinguished_name]
CN = localhost

[req_ext]
subjectAltName = @alt_names

[alt_names]
DNS.1 = localhost
IP.1 = 127.0.0.1
EOF
```

Then generate:

```bash id="k8k7c6"
openssl req -x509 -nodes -days 365 \
-newkey rsa:2048 \
-keyout certs/key.pem \
-out certs/cert.pem \
-config certs/openssl.cnf
```

---

# 🚀 Option 3 — Production (Let's Encrypt)

For real deployments, use a reverse proxy:

* Traefik
* Nginx
* Caddy

These tools can automatically generate and renew certificates.

👉 Example (recommended approach):

```text id="3n2a5b"
Client → HTTPS (Traefik/Nginx) → Adapter (HTTP)
```

Do NOT manually manage certificates in production unless necessary.

---

# 🔄 Regeneration

To regenerate certificates:

```bash id="f7c3he"
rm certs/*.pem
./generate-certs.sh   # if you created a helper script
```

---

# 🔒 Security Notes

* Never commit:

    * `key.pem`
    * real certificates
* Always keep private keys secure
* Rotate certificates if exposed

---

# 🧠 Summary

```text id="2q9wlc"
Local dev → self-signed certs
Production → reverse proxy + Let's Encrypt
```

---

# 🧑‍💻 Tip

If you're using Docker, mount this folder:

```yaml id="y1x6eq"
volumes:
  - ./certs:/certs
```

And configure your service to use:

```text id="k7q1mb"
/certs/cert.pem
/certs/key.pem
```

---

Ready to go 🚀
