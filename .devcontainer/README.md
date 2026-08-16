## Developing with Visual Studio Code + devcontainer

The easiest way to get started with custom integration development is to use Visual Studio Code with devcontainers. This approach will create a preconfigured development environment with all the tools you need.

In the container you will have a dedicated Home Assistant core instance running with your custom component code. You can configure this instance by updating the `./devcontainer/configuration.yaml` file.

**Prerequisites**

- [git](https://git-scm.com/book/en/v2/Getting-Started-Installing-Git)
- Docker
  -  For Linux, macOS, or Windows 10 Pro/Enterprise/Education use the [current release version of Docker](https://docs.docker.com/install/)
  -   Windows 10 Home requires [WSL 2](https://docs.microsoft.com/windows/wsl/wsl2-install) and the current Edge version of Docker Desktop (see instructions [here](https://docs.docker.com/docker-for-windows/wsl-tech-preview/)). This can also be used for Windows Pro/Enterprise/Education.
- [Visual Studio code](https://code.visualstudio.com/)
- [Remote - Containers (VSC Extension)][extension-link]

**YubiKey / GPG Support**

The devcontainer is pre-configured to use your YubiKey for SSH authentication and GPG signing:

- `gnupg`, `gpg-agent`, `pinentry`, `socat`, `openssh-client`, and `git` are installed in the container
- On Unix hosts, your host `~/.ssh` and `~/.gitconfig` are staged into `.devcontainer/host-config/` by the `initializeCommand` and then mounted into the container
- On Windows hosts, the installer stages your host `~/.ssh` and `~/.gitconfig` into `.devcontainer/host-config/`
- Your host `~/.gnupg` public key material is snapshotted into `.devcontainer/gnupg-host/` by the Windows/macOS installer and mounted into the container
- `yubikey-manager` CLI (`ykman`) is available for managing your YubiKey

**macOS setup**: Docker Desktop runs inside a Linux VM and cannot forward Unix domain
sockets (like `gpg-agent.ssh`) across the VM boundary. This devcontainer uses a TCP
relay to bridge the SSH and GPG agents from host to container.

1. **Install the relay as a macOS LaunchAgent** (runs at login, restarts automatically):
   ```sh
   sh .devcontainer/scripts/install-agent-relay.sh install
   ```
   This starts TCP relays on ports 9999 (SSH agent), 9998 (GPG agent), 9997 (scdaemon).

2. **Check status**:
   ```sh
   sh .devcontainer/scripts/install-agent-relay.sh status
   ```

3. **Open the devcontainer** in VS Code (`F1` → `Remote-Containers: Reopen in Container`).
   The `postCreateCommand` automatically starts the container-side socat bridge.

4. **Verify**: run `ssh -T git@github.com` and `gpg --card-status` in the container terminal.

To uninstall the relay:
   ```sh
   sh .devcontainer/scripts/install-agent-relay.sh uninstall
   ```

**Windows setup**: Docker Desktop on Windows cannot forward Windows named pipes or
GnuPG's emulated sockets into containers, so the same TCP relay architecture is used.
The Windows relay ([start-agent-relay.ps1](scripts/start-agent-relay.ps1)) is pure
PowerShell — no Python or other dependencies needed on the host.

Prerequisites:

- [Gpg4win](https://gpg4win.org/) (or another GnuPG for Windows) with the YubiKey
  already set up, and `%APPDATA%\gnupg\gpg-agent.conf` containing:
  ```
  enable-ssh-support
  enable-win32-openssh-support
  ```
  The second option makes gpg-agent serve the `\\.\pipe\openssh-ssh-agent` named
  pipe, which the relay forwards to the container on port 9999.
- A shell such as Git Bash or WSL2 so the devcontainer can run
  `.devcontainer/scripts/initialize-host.sh` during container initialization.
- Docker Desktop for Windows.

Setup steps:

1. **Install the relay as a per-user Scheduled Task** (starts at logon, restarts on
   failure, no admin required). Run in PowerShell from the repository root:
   ```powershell
   powershell -NoProfile -ExecutionPolicy Bypass -File .devcontainer\scripts\install-agent-relay.ps1 install
   ```
   This starts TCP relays on ports 9999 (SSH agent), 9998 (GPG agent), 9997 (scdaemon),
   stages `%USERPROFILE%\.ssh` and `%USERPROFILE%\.gitconfig` into
   `.devcontainer/host-config/` (git-ignored), and snapshots your public GnuPG key
   material (`%APPDATA%\gnupg`) into `.devcontainer/gnupg-host/` (git-ignored) so the
   container can verify signatures and know which keys live on the YubiKey. Re-run
   `install` after changing SSH/Git config or adding new keys.

2. **Check status**:
   ```powershell
   powershell -NoProfile -ExecutionPolicy Bypass -File .devcontainer\scripts\install-agent-relay.ps1 status
   ```

3. **Open the devcontainer** in VS Code (`F1` → `Dev Containers: Reopen in Container`).
   The `postCreateCommand` automatically starts the container-side socat bridge.

4. **Verify**: run `ssh -T git@github.com` and `gpg --card-status` in the container terminal.

To uninstall the relay:
   ```powershell
   powershell -NoProfile -ExecutionPolicy Bypass -File .devcontainer\scripts\install-agent-relay.ps1 uninstall
   ```

> **Note:** Signing commits or authenticating may require touching the YubiKey or
> entering its PIN — the pinentry prompt appears on the Windows host, not in the
> container.

**macOS filesystem performance**

Docker Desktop on macOS runs containers inside a Linux VM, so the default bind-mounted workspace is slower than the native container filesystem. This devcontainer mitigates that by:

- Mounting the workspace with `consistency=cached` to reduce cross-VM sync overhead.
- Storing Python tool caches (`__pycache__`, `.mypy_cache`, `.pytest_cache`, `.ruff_cache`, `uv`) in a named Docker volume (`hsem-cache`) instead of inside the bind-mounted workspace.

These changes are automatic after rebuilding the container. You may see an empty `.cache` folder on your Mac; this is expected and harmless — the actual cache data lives inside the Docker volume.

**Linux note**: On a native Linux Docker host, you can add `--privileged` and USB
device mounts via `runArgs` in `devcontainer.json` for direct hardware access.

[More info about requirements and devcontainer in general](https://code.visualstudio.com/docs/remote/containers#_getting-started)

[extension-link]: https://marketplace.visualstudio.com/items?itemName=ms-vscode-remote.remote-containers

**Getting started:**

1. Fork the repository.
2. Clone the repository to your computer.
3. Open the repository using Visual Studio code.

When you open this repository with Visual Studio code you are asked to "Reopen in Container", this will start the build of the container.

_If you don't see this notification, open the command palette and select `Remote-Containers: Reopen Folder in Container`._

### Tasks

The devcontainer comes with some useful tasks to help you with development, you can start these tasks by opening the command palette and select `Tasks: Run Task` then select the task you want to run.

When a task is currently running (like `Run Home Assistant on port 8123` for the docs), it can be restarted by opening the command palette and selecting `Tasks: Restart Running Task`, then select the task you want to restart.

The available tasks are:

Task | Description
-- | --
Run Home Assistant on port 8123 | Launch Home Assistant with your custom component code and the configuration defined in `.devcontainer/configuration.yaml`.

### Step by Step debugging

With the development container,
you can test your custom component in Home Assistant with step by step debugging.

You need to modify the `configuration.yaml` file in `.devcontainer` folder
by uncommenting the line:

```yaml
# debugpy:
```

Then launch the task `Run Home Assistant on port 8123`, and launch the debugger
with the existing debugging configuration `Python: Attach Local`.

For more information, look at [the Remote Python Debugger integration documentation](https://www.home-assistant.io/integrations/debugpy/).
