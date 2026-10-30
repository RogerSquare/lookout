# Install the hub

The Lookout Hub is one Docker container. It runs on 64-bit Intel/AMD and ARM machines with at
least 2 GB of free memory. Pick your machine:

- [Linux, Raspberry Pi or a NAS with SSH](#linux-raspberry-pi-nas)
- [Synology NAS (Container Manager)](#synology-nas)
- [Windows](#windows)
- [Mac](#mac)

When the hub starts for the first time it tests how fast the machine is and picks the person
detector that suits it. That takes about a minute.

## Linux, Raspberry Pi, NAS

Install Docker if you don't have it (`curl -fsSL https://get.docker.com | sh` on most Linux
systems, including Raspberry Pi OS 64-bit). Then run:

```sh
curl -fsSL https://github.com/RogerSquare/lookout/releases/latest/download/install.sh | sh
```

The hub installs into `./lookout-hub`, starts, and prints a link like
`http://192.168.1.20:8765/?setup=ABCD-EFGH-JKLM`. Open it in a browser on your phone or computer
and choose a password. That's the hub's own password: nobody else has one.

Prefer to do it by hand? Download
[compose.yml](https://github.com/RogerSquare/lookout/releases/latest/download/compose.yml) into a
folder and run `docker compose up -d` there.

## Synology NAS

1. In **Package Center**, install **Container Manager**.
2. In **File Station**, make a folder `docker/lookout-hub`, and inside it a folder `data`.
3. Download [compose.yml](https://github.com/RogerSquare/lookout/releases/latest/download/compose.yml)
   and upload it into `docker/lookout-hub`.
4. In **Container Manager**, open **Project**, choose **Create**, name it `lookout-hub`, set the
   path to `docker/lookout-hub`, and choose **Use existing docker-compose.yml**. Finish the wizard.
5. Open **Container** and select `lookout-hub`, then **Log**. Look for the line that starts with
   `NEW HUB:` and open the link in it.

Other NAS systems with a "Docker Compose" or "Stack" screen (QNAP Container Station, Unraid,
TrueNAS, Portainer) work the same way: paste in the contents of `compose.yml`.

## Windows

1. Install [Docker Desktop](https://www.docker.com/products/docker-desktop/) and start it.
2. Open **PowerShell** and run:

```powershell
irm https://github.com/RogerSquare/lookout/releases/latest/download/install.ps1 | iex
```

It installs into your user folder, allows the hub through Windows Firewall (Windows asks you to
confirm), and opens the setup page. In Docker Desktop's settings, turn on **Start Docker Desktop
when you sign in** so the hub comes back after a restart. The PC has to be on for alerts to work,
so a NAS or Raspberry Pi is a better home for the hub if you have one.

## Mac

Install [Docker Desktop](https://www.docker.com/products/docker-desktop/), start it, then in
Terminal:

```sh
curl -fsSL https://github.com/RogerSquare/lookout/releases/latest/download/install.sh | sh
```

On Windows and Mac the apps can't find the hub on the network by themselves, so pair them by
scanning the QR code on the setup page (that's the easiest way anyway).

## Updating

In the folder the hub is installed in:

```sh
docker compose pull && docker compose up -d
```

(On Windows and Mac add `-f compose.desktop.yml` after `docker compose` in both commands.) Your
settings, cameras and paired devices are kept in the `data` folder and survive updates.

## Next

[Add your cameras](cameras.md).
