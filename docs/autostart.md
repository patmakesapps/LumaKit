# Autostart (systemd)

LumaKit can run automatically on boot via a systemd service.

## Setup

1. Copy the example service file and edit it with your paths:
   ```bash
   cp lumakit.service.example lumakit.service
   # edit lumakit.service — set User and paths
   ```

2. Install and enable:
   ```bash
   sudo cp lumakit.service /etc/systemd/system/lumakit.service
   sudo systemctl daemon-reload
   sudo systemctl enable lumakit.service
   ```

3. Start it now (or just reboot):
   ```bash
   sudo systemctl start lumakit.service
   ```

## Common Commands

| Command | Purpose |
|---|---|
| `sudo systemctl status lumakit` | Check if running |
| `sudo journalctl -u lumakit -f` | Live logs |
| `sudo systemctl start lumakit` | Start the service |
| `sudo systemctl stop lumakit` | Stop the service |
| `sudo systemctl restart lumakit` | Restart the service |
| `sudo systemctl enable lumakit` | Enable autostart on boot |
| `sudo systemctl disable lumakit` | Disable autostart on boot |

## Manual Testing

Stop the service first, then run manually as usual:
```bash
sudo systemctl stop lumakit
python3 main.py
```

When done testing, start the service again:
```bash
sudo systemctl start lumakit
```
