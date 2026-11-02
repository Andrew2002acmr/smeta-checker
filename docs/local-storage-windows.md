# Local storage on this Windows PC

Docker Desktop uses the WSL 2 disk image location configured in its settings:

`D:\VOR\docker-desktop\DockerDesktopWSL`

The managed `disk/docker_data.vhdx` holds container images, writable layers,
PostgreSQL and Redis volumes, original documents, OCR artifacts and PDF caches.
Keep the existing named volumes: changing Compose to empty host directories
would hide the current data rather than migrate it.

The host WSL configuration at `%USERPROFILE%\.wslconfig` directs its swap file
to `D:\VOR\wsl\swap.vhdx`. This setting requires a complete WSL VM restart.
Docker Desktop's own small settings and diagnostic logs remain in the Windows
user profile on C:.

`compose.local.yaml` limits each service's container logs to three 10 MB files.
Run with both Compose files and `--env-file .env.local` as usual. Applying a
changed logging configuration recreates containers, preserving named volumes.

Before the 2026-10-28 relocation, the stopped Docker data disk was copied to
`D:\VOR\backups\docker-storage-before-move-20261028\docker_data.vhdx`.
The source and backup SHA-256 matched:
`34ac0140ba2bba14077f6e59ede861e03a6a22440ece0360152d8a600907e475`.
That is a point-in-time backup, not the active disk. Do not attach it while
Docker is running or overwrite the active disk with it.

Docker's UI relocation failed to remove its original disk and reverted its
setting. After rollback, the C: disk unexpectedly contained an older state
(9 runs, schema 0008), while the copy made by the UI retained the pre-move
state (12 runs, schema 0009). Docker Desktop was fully stopped and WSL shut
down before switching disks. `CustomWslDistroDir` points to the directory
above, and the UI's copied disk is now the active disk. Its SHA-256 before
startup was `99e2853ad4f3627e740b4bfb06f112c0677edf76a6b6cb706540286f022d92ec`.
The original pre-move backup remains untouched. The stale rollback disk is
retained as `docker-data-stale-after-ui-rollback.vhdx` in the backup directory.
Do not restore that stale disk over the working disk.

To relocate again, use Docker Desktop Settings > VM resources > Disk image
location (older versions: Resources > Advanced). Back up first, let Docker
perform the move, then verify containers, database counts and document access.
Do not unregister WSL distributions or delete volumes to move storage.
