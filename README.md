<div align="center">

# 🎛️ Playbook

### A small control desk for a small rack.

🥧 Raspberry Pi &nbsp;·&nbsp; 🖥️ Mini PCs &nbsp;·&nbsp; 🐳 Docker Desktop &nbsp;·&nbsp; ☸️ Kubernetes &nbsp;·&nbsp; 🛰️ Tailscale

![Status: evolving toolkit](https://img.shields.io/badge/status-evolving%20toolkit-orange)
![Runs in Docker Compose](https://img.shields.io/badge/runs%20in-Docker%20Compose-2496ED)
![Provisioning: cloud-init + Ansible](https://img.shields.io/badge/provisioning-cloud--init%20%2B%20Ansible-6f42c1)

**Browser-based rack bootstrap and day-to-day operations for SOHO racks.**<br/>
Start small · bring devices online deliberately · keep a private path back in.

</div>

> [!TIP]
> 🧭 **New here?** Read the [project vision](docs/soho-rack-vision.md) first: the
> mini-PC control desk, remote access, router setup, and why local Kubernetes and
> the physical rack are kept apart.

---

<a id="the-idea-at-a-glance"></a>
## 🧭 The idea at a glance

```mermaid
flowchart LR
    Operator["👤 Remote operator"] -->|"🛰️ Private Tailscale access"| Desk["🖥️ NUC / mini-PC<br/>desktop + browser"]
    Desk --> Desktop["🐳 Docker Desktop<br/>portal + local Kubernetes"]
    Desk -->|"🔌 Authorized management path"| Network["📶 Router + switch"]
    Desktop -->|"📡 Provision and monitor"| Rack["🥧 Rack nodes<br/>K3s workers or utility hosts"]
    Network --- Rack
```

> [!NOTE]
> This is the **intended topology**, not automatic network configuration. Desktop
> sharing, tailnet permissions, container reachability, and router access are all
> configured explicitly by you.

<a id="whats-in-the-box"></a>
## 🧩 What's in the box

```mermaid
mindmap
  root(("🎛️ Playbook"))
    🥧 Bootstrap
      SD images for Pi 5 / Pi 4 / Pi 3B+
      cloud-init templates
      📦 image catalog + cache
    📡 Operate
      callback history
      📊 Hardware Monitor
      🎛️ Node Ops
      🖥️ SSH diagnostics
      📜 boot logs
    🛠️ Configure
      reusable node modules
      Ansible roles
      OpenWrt SSH onboarding
      🛰️ Tailscale policy planner
    ☸️ Kubernetes
      Docker Desktop + Helm
      rack K3s path
    💾 Flash, opt-in
      Linux flash helper
      unattended UEFI installer USB
```

<a id="life-of-a-node"></a>
## 🔁 Life of a node

```mermaid
sequenceDiagram
    autonumber
    actor Op as 👤 Operator
    participant P as 🎛️ Portal
    participant SD as 💾 SD card
    participant N as 🥧 Node
    Op->>P: Pick profile, image, cloud-init options
    P->>SD: Flash image, inject + verify cloud-init
    Op->>N: 🔌 Insert card and power on
    N->>N: ☁️ cloud-init first boot
    N-->>P: 📡 Callback, then 📜 boot logs
    P->>N: 🎛️ Automatic first check, Node Ops job
    Op->>P: 🎛️ Node Ops: check, then apply modules
    P->>N: 🅰️ Ansible over the preferred route
    loop Every power restore
        N-->>P: 📜 Boot logs
    end
```

<a id="boundaries"></a>
## 🚫 Boundaries

| ✅ It is | 🚫 It is not |
| --- | --- |
| An evolving operator toolkit | A turnkey router installer |
| A way to bring nodes up one at a time | A production high-availability platform |
| A Docker Desktop Kubernetes workbench | A cluster that absorbs your Pis automatically |
| A container with persistent state | A container that sees the host's Tailscale or USB/SD devices by default |

<a id="start-the-portal"></a>
## 🚀 Start the portal

With Docker and Compose available, from the repository root:

```bash
docker compose up --build -d
docker compose ps
```

Then open **<http://127.0.0.1:8088/>** on the host. 🎉

This starts the portal only — it does not configure the router or provision nodes.

> [!WARNING]
> 🔐 **Keep management private.** Base Compose publishes the portal port on host
> interfaces. Restrict it with host firewall rules to trusted LAN/tailnet paths and
> **never port-forward the dashboard to the internet.**

> [!IMPORTANT]
> 💾 State lives in the `callback-site-data` volume. It survives rebuilds, but it is
> **not a backup**.

For host setup, Kubernetes connectivity, credentials, and device-access limits, see
the [operator deployment guide](callback_site/README.md#deployment-notes).
Before committing deployment changes, install the
[secret checks and read the credential-handling guide](SECURITY.md). 🔐

<a id="choose-your-next-step"></a>
## 🗺️ Choose your next step

```mermaid
flowchart TD
    Start(["🤔 What do you want to do?"]) --> Why{"Understand<br/>or build?"}
    Why -->|"Understand"| Vision["🧭 SOHO rack vision"]
    Why -->|"Build"| Where{"Where?"}
    Where -->|"On the desk"| Portal["🐳 Run the portal"]
    Portal --> K8s["☸️ Docker Desktop<br/>Kubernetes + Helm"]
    Where -->|"In the rack"| Runbook["🥧 Rack cluster runbook"]
    Runbook --> Tailnet["🛰️ Tailscale policy"]
    Runbook --> Toolkit["🧰 Network Toolkit Pi"]
    Where -->|"On the code"| Dev["🛠️ Operator guide +<br/>functional contracts"]
```

| I want to... | Read |
| --- | --- |
| 🧭 **Understand the purpose and architecture** | [SOHO rack vision](docs/soho-rack-vision.md) |
| 🐳 **Run the portal with Docker Desktop** | [Docker Compose setup](callback_site/README.md#docker-compose) |
| ☸️ **Connect the local Kubernetes cluster** | [Docker Desktop Kubernetes and Helm](callback_site/README.md#docker-desktop-kubernetes-and-helm) |
| 🥧 **Bring up and maintain rack nodes** | [Rack cluster runbook](docs/rack-cluster-runbook.md) |
| 🛰️ **Plan private management access** | [Tailscale policy workflow](docs/rack-cluster-runbook.md#tailscale-policy-page) |
| 🧰 **Use a dedicated network toolkit device** | [Network Toolkit setup](callback_site/README.md#network-toolkit-pi-3b) |
| 💾 **Flash cards or build installer USBs from Linux** | [Isolated flash helper](flash_helper/README.md) |
| 🛠️ **Understand or develop the portal** | [Operator guide](callback_site/README.md) · [Functional contracts](docs/callback_site-functional-prototypes.md) |
| 🔐 **Handle credentials safely** | [Secrets and repository safety](SECURITY.md) |

<a id="repository-map"></a>
## 🗂️ Repository map

```mermaid
flowchart LR
    subgraph runtime["🐳 Runtime"]
        compose["compose*.yaml"]
        portal["🎛️ callback_site/"]
        helper["💾 flash_helper/<br/>+ charts/"]
    end
    subgraph automation["🛠️ Automation"]
        scripts["📜 scripts/<br/>SD + cloud-init prep"]
        ansible["🅰️ roles/ · library/<br/>site.yml · inventory.yml<br/>group_vars/ · host_vars/"]
    end
    cloudinit["☁️ cloud-init/<br/>reference examples"]
    compose --> portal
    compose -.->|"opt-in"| helper
    portal --> scripts
    portal -->|"Node Ops"| ansible
    portal -.->|"opt-in"| helper
```

| Location | What belongs here |
| --- | --- |
| 🎛️ [callback_site/](callback_site/) | Web portal, services, templates, and tests. |
| 📚 [docs/](docs/) | Project vision, rack operations, and functional reference. |
| 📜 [scripts/](scripts/) | Bootstrap, SD preparation, validation, and local Kubernetes helpers. |
| ☁️ [cloud-init/](cloud-init/) | Reference cloud-init examples (the portal generates per-node user-data). |
| 🅰️ [roles/](roles/) · [library/](library/) | Ansible provisioning for rack devices and the network toolkit. |
| 📋 [inventory.yml](inventory.yml) · [site.yml](site.yml) | Device inventory and the main provisioning playbook. |
| 💾 [flash_helper/](flash_helper/) · [charts/](charts/) | Opt-in isolated Linux flashing helper and its Helm chart. |
| 🐳 [compose.yaml](compose.yaml) · [compose.kubernetes.yaml](compose.kubernetes.yaml) · [compose.flash-helper.yaml](compose.flash-helper.yaml) | Portal runtime, optional kubeconfig mount, optional flash-helper wiring. |

---

<div align="center">

**🧭 Working principle:** establish local control → verify private remote access →
automate one device at a time.

</div>
