# Autostart (systemd)

LumaKit can run automatically on boot via a systemd service. The service should
run the unified backend entrypoint, not a single surface.

For the day-to-day launcher commands, see [launcher.md](launcher.md).
If you have not installed the repo CLI yet, run the same service command as `python3 -m lumakit service install --force`.

## Setup

1. Generate a service file for your current checkout:
   ```bash
   lumakit service install --force
   ```

2. Review `lumakit.service`, then install and enable it:
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
lumakit open
```

When done testing, start the service again:
```bash
sudo systemctl start lumakit
```
