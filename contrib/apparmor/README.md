# contrib/apparmor

`wristpull-cli` – the AppArmor profile the Docker `cli` service runs under (`security_opt:
apparmor:wristpull-cli` in `compose.yaml`). It is Docker's default container profile plus one rule:
D-Bus traffic on the system bus, which bleak needs to reach the host's BlueZ for `wristpull pull
strap`. Everything else Docker denies (writes under `/proc` and `/sys`, mounts, …) stays denied;
`web` keeps `docker-default`.

```sh
sudo install -m 0644 contrib/apparmor/wristpull-cli /etc/apparmor.d/wristpull-cli
sudo apparmor_parser -r -W /etc/apparmor.d/wristpull-cli      # load now; /etc/apparmor.d loads it at boot
aa-status | grep wristpull-cli                                # should list the profile
docker compose run --rm cli wristpull pull strap --dry-run
```

Without the profile loaded, Docker refuses to start the `cli` container (`apparmor profile not
found`): either install it, or drop the `security_opt` lines from the `cli` service to fall back to
`docker-default` (then everything but the strap works). The strap also needs the host user to be
allowed to use BlueZ: a logged-in desktop session (`at_console`) or membership in the `bluetooth`
group.

Based on moby's `profiles/apparmor/template.go`; re-check that template after a Docker upgrade that
changes the default profile.
