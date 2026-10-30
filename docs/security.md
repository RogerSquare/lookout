# How Lookout keeps your cameras private

**No accounts and no cloud.** Your video goes from the cameras to your hub and from the hub to your
own devices. There's no Lookout server in between and nothing to sign up for.

**The setup page has its own password**, chosen when you first open it with the one-time setup code
from the hub's log. After five wrong passwords in a minute it stops accepting tries for a minute.
The page only answers on your home network.

**Every phone and watch has its own key.** When you pair a device, it makes a key that never
leaves it (on iPhone and Apple Watch it's kept in the Secure Enclave) and registers only the public
half with the hub. Every request the app makes is signed with that key; requests that are copied,
replayed or more than two minutes old are refused. **Remove** on the setup page shuts a device out
at once, and its alerts stop.

**Pairing codes work once, for 10 minutes.** The 6-digit form only works on your home network;
from the internet only the full code in the QR code does. Five wrong codes in a minute pause
pairing for a minute.

**The hub has its own certificate.** At home, the apps talk to the hub over HTTPS using a
certificate the hub made for itself. The QR code carries that certificate's fingerprint, and the
apps refuse any other, so another device on your network can't pretend to be your hub.

**Video links expire.** Video players open streams by link, so those links carry a token that
works for 10 minutes and only while the device is still paired.

**Away from home**, only the app API is reachable through a tunnel, and only for paired devices.
See [away from home](away.md).

## What your home network can see

Devices on your home network can reach the hub's live alerts feed and the camera restreams on port
8555 without a password. That's what lets Lookout on a PC or TV, or an NVR, use them. If you don't
trust everything on your network (guests, smart-home gadgets), put the cameras and the hub on
their own network or VLAN.

## The old shared key

Early test versions of the apps all used one shared key. A hub upgraded from those keeps accepting
it until you turn off **Accept the shared app key** on the **Apps** page. New hubs never accept it.

## Found a problem?

Please report security problems privately through
[GitHub security advisories](https://github.com/RogerSquare/lookout/security/advisories/new)
rather than in a public issue.
