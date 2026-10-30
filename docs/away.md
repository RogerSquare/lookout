# See your cameras away from home

At home the apps talk straight to the hub. To use them on mobile data or someone else's Wi-Fi,
the hub needs an address that works from the internet. Pick one:

## Option 1: Cloudflare Tunnel (free, needs a domain)

The hub stays hidden behind Cloudflare and nothing on your router changes.

1. Add your domain to a free [Cloudflare](https://dash.cloudflare.com) account.
2. In **Zero Trust**, open **Networks > Tunnels**, choose **Create a tunnel** (Cloudflared), and
   run the connector command it gives you on the hub's machine (or any machine at home).
3. Under **Public Hostname**, add:
   - **Subdomain/domain**: for example `lookout.example.com`
   - **Path**: `^/api/watch/`
   - **Service**: `HTTP` and `<hub address>:8765`
4. Tell the hub its away address. On the hub's machine:

   ```sh
   docker exec lookout-hub python -c "import json,urllib.request as u;t=open('/data/admin-token').read().strip();r=u.Request('http://127.0.0.1:8765/api/admin/settings',method='PUT',data=json.dumps({'remote_url':'https://lookout.example.com'}).encode(),headers={'Authorization':'Bearer '+t,'Content-Type':'application/json'});print(u.urlopen(r).read().decode())"
   ```

5. Pair the apps again (or pair them after this step): the QR code now carries the away address.

The path rule matters: only the app API goes through the tunnel. The setup page, the alerts feed
for desktop apps and everything else answer "not found" from the internet, and every app request
must be signed by a paired device.

## Option 2: Tailscale (free, no domain)

Install [Tailscale](https://tailscale.com) on the hub's machine and on your iPhone. Turn Tailscale
on when you're away and the apps reach the hub as if you were home. Apple Watch uses the iPhone's
connection, so it works while the iPhone is nearby.

## Option 3: VPN to your router

If your router runs a VPN server (WireGuard, OpenVPN), connecting to it works like Tailscale.
