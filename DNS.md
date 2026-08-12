# Going live on intake.bclworkspace.in (and off DuckDNS)

The tool was reachable over a DuckDNS hostname. It now runs on the company
subdomain **`intake.bclworkspace.in`**.

DNS for `bclworkspace.in` is hosted at **GoDaddy** (nameservers
`ns43.domaincontrol.com` / `ns44.domaincontrol.com`), so whoever has the GoDaddy
login for that domain is the person who can make this change. `intake` is
currently unused, so nothing gets overwritten.

## 1. What to send them

> Please add one record to the DNS zone for **bclworkspace.in** — a plain A
> record, not Domain Forwarding:
>
> | Field | Value |
> | --- | --- |
> | Type | `A` |
> | Name / Host | `intake` |
> | Value / Points to | `69.62.76.222` |
> | TTL | `600` (10 min) — GoDaddy's lowest; raise to 1 hour once confirmed |
>
> Nothing else changes. The apex `bclworkspace.in`, `www`, and all mail records
> (MX / SPF / DKIM / DMARC) stay exactly as they are — this is purely additive.
>
> Please let us know once it is saved so we can issue the certificate.

`69.62.76.222` is the Hostinger VPS. Ports 80 and 443 are already open on it and
nginx is answering, so there is nothing for the DNS side to confirm beyond the
record itself.

No `AAAA` record: `bclworkspace.in` has no IPv6 today, and a stale AAAA would
break the site for IPv6 clients. Only add one if the server genuinely has a
public IPv6 address.

## 2. Once they confirm — our side

The server runs **nginx** on 80/443 and the app container publishes to
**127.0.0.1:8011** only. So nginx is the reverse proxy — do *not* use
`docker-compose.prod.yml` (that overlay starts Caddy, which cannot bind ports
nginx already holds).

Check it resolves to our IP (allow ~10 min for the TTL):

```bash
nslookup intake.bclworkspace.in 8.8.8.8
```

Set both values in `.env`, then restart the app so it picks them up:

```
BASE_URL=https://intake.bclworkspace.in
SITE_ADDRESS=intake.bclworkspace.in
```

```bash
docker compose up -d
```

`BASE_URL` is what gets baked into the client links the dashboard generates, and
it must be `https://` — the app only marks its session cookie secure when it is.
(`SITE_ADDRESS` is only read by the Caddy overlay; harmless to set either way.)

Install the vhost from `deploy/nginx/intake.bclworkspace.in.conf`:

```bash
sudo cp deploy/nginx/intake.bclworkspace.in.conf /etc/nginx/sites-available/
sudo ln -s /etc/nginx/sites-available/intake.bclworkspace.in.conf /etc/nginx/sites-enabled/
sudo nginx -t && sudo systemctl reload nginx
```

Then issue the certificate — certbot edits the vhost in place, adding the
`ssl_*` directives and the http->https redirect:

```bash
sudo certbot --nginx -d intake.bclworkspace.in
```

Confirm:

```bash
curl -I https://intake.bclworkspace.in/admin/login
```

Expect `HTTP/2 200` and no certificate warning. Two settings in that vhost exist
for a reason and should survive any future edit: `client_max_body_size 60m`
(nginx's 1 MB default rejects document uploads with a bare 413) and the 300s
proxy timeouts (OCR runs inline, and nginx's 60s default would cut a slow
extraction off mid-request).

## 3. Disconnecting from DuckDNS

Do this **after** the new domain is confirmed working, not before — and note
that any intake link already sent to a client on the DuckDNS host stops working
the moment DuckDNS stops resolving. The token in the link is still valid on the
new domain, so either re-send those clients their link with the new URL, or keep
DuckDNS pointing at the server for a week while they finish.

On the server, in order:

1. **Stop the IP updater.** DuckDNS keeps a hostname pointed at a changing IP
   via a cron job or a systemd timer that curls their update URL. Find it:

   ```bash
   crontab -l | grep -i duckdns
   sudo grep -ril duckdns /etc/cron.d /etc/systemd/system /root /home 2>/dev/null
   ```

   Remove the cron line (`crontab -e`), or if it is a systemd timer:

   ```bash
   sudo systemctl disable --now duckdns.timer
   ```

   Also delete the updater script and any file holding the DuckDNS token — that
   token can repoint the hostname, so it should not linger on disk.

2. **Take the hostname out of nginx.** Remove any `server` block whose
   `server_name` is the DuckDNS host, then reload:

   ```bash
   sudo grep -ril duckdns /etc/nginx/
   sudo nginx -t && sudo systemctl reload nginx
   ```

   Leaving it in means certbot keeps trying to renew a certificate for a
   hostname that no longer resolves, and the renewal timer starts failing. Drop
   that cert too:

   ```bash
   sudo certbot delete --cert-name <duckdns-hostname>
   ```

3. **Delete the domain at DuckDNS.** Log in at duckdns.org and remove the
   hostname from the account. Until you do, the name still exists and someone
   else's IP could end up behind it.

4. **Re-check `.env`.** Make sure no `BASE_URL` or `SITE_ADDRESS` anywhere still
   mentions duckdns:

   ```bash
   grep -ri duckdns /path/to/extraction_tool
   ```

5. **Regenerate any outstanding client links** from the dashboard so they carry
   the new host, and confirm one end to end in a browser before telling clients.

## Why this subdomain

`intake` says what the link is for, and the client sees the full URL in whatever
we send them — it needs to read as an official company address rather than
something generic, or it gets treated as phishing. Alternatives if the team
prefers: `forms.`, `onboarding.`, `client.`. Avoid hyphens and abbreviations for
the same reason.

Note that the logo and the "A service of" link on the form still point at
**bclindia.in**, since that is the public-facing company site. Only the address
the tool is *served* from moves to `bclworkspace.in`. Say the word if the form
should link to bclworkspace.in instead.
