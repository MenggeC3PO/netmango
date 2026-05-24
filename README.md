<p align="center">
  <img src="assest/Netmango_fullname.png" alt="Netmango" width="420">
</p>

<p align="center">
  <em>A user-friendly network emulator.</em>
</p>

---

## Why Netmango?

This started back when I was a fresh ICT master's student, helping out on a
remote-controlled vehicle project. The robot talked to its operator over
Wi-Fi, and on paper everything worked great. However, but the moment we drove it a
little further away, or around an obstacle, the robot started behaving
funny.

To make the control loop more robust, we needed a way to **reproduce those
messy links on demand**. Although Linux has a
`netem` for this, but it turns out very unfriendly to use:
long commands, easy to mistype, and if you forget to undo your rule your
Wi-Fi just stays broken.

So I built **Netmango**: a small, friendly Python app that lets you
emulate and monitoring the messy real-world network conditions your device will actually
face out in the wild 🥭. With it you can:

- pick the interface you want to mess with,
- add delay, jitter, packet loss, and corruption with a few clicks,
- watch your link quality and throughput live while you test,
- and just close the window when you're done — Netmango cleans up after
  itself, no leftover rules.

Say goodbye to wrestling with `netem`.

---

## APP Screenshot
<p align="center">
  <img src="assest/Software_ui.png" alt="Netmango_app" width="420">
</p>

---

## Quick start

```bash
# Clone the repo
cd netmango
./start.sh
```

That's it. `start.sh` will:

1. Create `.venv/` if it doesn't exist.
2. Install / update the Python dependencies on the first run.
3. Launch the GUI.

To stop, just click the **×**. Netmango automatically removes any rule it
applied.

---

## Prerequisites

Netmango runs on Linux and relies on a few common system packages.

### System packages

On Debian / Ubuntu:

```bash
sudo apt update
sudo apt install -y \
    python3 python3-venv python3-pip \
    iproute2 iputils-ping \
    libxcb-cursor0 libxcb-xinerama0
```

### Python packages

Listed in [`requirements.txt`](requirements.txt) — installed automatically
by `start.sh` into a local `.venv/`.

### Sudo

Netmango uses `tc`, which is a Linux kernel feature and needs root, so it
will prompt for your sudo password the first time you apply a rule.

---

## Author

I'm **Mengge Zhang**, an ICT master's student at KU Leuven. Netmango
is a personal project, fully open source. I like to use what I learned to solve realworld challenges. Happy shaping! 🥭

I'm currently looking for opportunities to pursue a PhD, so feel free to get in touch!
