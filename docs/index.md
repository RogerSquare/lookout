# Lookout

See who's at the door on your Apple Watch.

Lookout watches your cameras for people. When someone shows up, your watch and iPhone get an alert
with a picture, and the camera is one tap away, live. Cars, pets, shadows and rain don't count.

Everything runs at home on a small server called the **Lookout Hub**. Your video never goes to
anyone else's cloud.

## What you need

- **Cameras** that offer an RTSP stream (most Wyze with RTSP firmware, Reolink, Amcrest, Hikvision,
  Dahua, TP-Link Tapo and UniFi Protect cameras do), **or** a [Frigate](https://frigate.video) NVR.
- **A computer that stays on** to run the hub: a NAS, a Raspberry Pi 4 or 5, an old PC or a Mac.
  It needs [Docker](https://docs.docker.com/get-docker/).
- **An iPhone** with the Lookout app. An **Apple Watch** is optional but it's the best part.

## Set it up

1. [Install the hub](install.md). One command, about five minutes.
2. [Add your cameras](cameras.md) on the hub's setup page, or connect it to Frigate.
3. [Pair your iPhone and Apple Watch](apps.md) by scanning a QR code.
4. Optional: [see your cameras away from home](away.md).

## More

- [How Lookout keeps your cameras private](security.md)
- [Troubleshooting](troubleshooting.md)
- [Lookout on your PC and TV](desktop-and-tv.md)
- [Source code](https://github.com/RogerSquare/lookout)
