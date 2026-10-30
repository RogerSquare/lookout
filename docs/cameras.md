# Add your cameras

Open the hub's setup page (`http://<hub address>:8765`) and sign in.

## Cameras with an RTSP stream

On **Cameras**, choose **Find cameras** to look for cameras on your network, or **Add camera** and
fill in:

- **Name**, for example *Front door*.
- **Stream address**: the camera's RTSP link, usually
  `rtsp://username:password@<camera address>:554/<path>`. Your camera's app or manual lists it;
  search for "*camera model* RTSP URL" if not. Choose **Test stream** to check it.
- **Sub-stream address** (optional): a smaller second stream many cameras offer. The hub uses it
  for detection, which saves a lot of processing.
- **This camera watches a door** makes alerts say "Someone's at the door".

Once the camera is saved, draw **where to look**: click around the area people have to stand in
(the porch, not the street) to outline it. **Ignore people shorter than** skips people far away,
such as passers-by on the pavement.

The hub connects to each camera once and shares that one stream with everything else, so the
cameras aren't overloaded however many phones and watches are watching.

## Using Frigate

Already run [Frigate](https://frigate.video)? Let it do the detecting and Lookout does the alerts
and the watch. On **Detection**, choose **Frigate** and enter its address:

- Frigate on the same machine as the hub: `http://127.0.0.1:5000` (no login needed).
- Frigate on another machine: `https://<frigate address>:8971` with a Frigate user and password.

Lookout picks up Frigate's cameras and alerts when Frigate raises them, and the apps show
Frigate's recent visitors with their snapshots. Choose **Alerts and detections** if you want an
alert for every person Frigate sees, not only those in its alert zones.

## Detection on the hub

Without Frigate, the hub runs its own person detector
([RF-DETR](https://github.com/roboflow/rf-detr)). On **Detection** you can choose between
**Save power**, **Balanced** and **Most accurate**; the hub picks the model and how often to check
from what this machine can manage. **Test this hardware again** re-runs the speed test, for
example after moving the hub to a faster machine.

## Next

[Pair your iPhone and Apple Watch](apps.md).
