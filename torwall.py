
#!/usr/bin/env python3

import os
import sys
import subprocess
import shutil
import json
from pathlib import Path

# ==========================================
# CONFIGURATION
# ==========================================

TOR_UID = 122
TOR_TRANSPORT = 9040
TOR_DNS = 5353

CONFIG_DIR = Path("/etc/torwall")
BACKUP_DIR = CONFIG_DIR / "backup"

TORRC_PATH = Path("/etc/tor/torrc")
SYSCTL_BACKUP = BACKUP_DIR / "sysctl.json"


# ==========================================
# SYSTEM
# ==========================================


def run_cmd(*args):
    subprocess.run(
        list(args),
        check=True,
        stdout=subprocess.DEVNULL
    )


def create_chain(table, chain):
    subprocess.run(
        ["iptables", "-w", "-t", table, "-N", chain],
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL
    )

    run_cmd("iptables", "-w", "-t", table, "-F", chain)


def attach_chain(table, chain):
    result = subprocess.run(
        ["iptables", "-w", "-t", table, "-C", "OUTPUT", "-j", chain],
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL
    )

    if result.returncode != 0:
        run_cmd("iptables", "-w", "-t", table, "-I", "OUTPUT", "1", "-j", chain)


def configure_nat():
    create_chain("nat", "TORWALL_NAT")

    # O próprio Tor não deve ser redirecionado
    run_cmd(
        "iptables", "-w", "-t", "nat", "-A", "TORWALL_NAT",
        "-m", "owner", "--uid-owner", str(TOR_UID),
        "-j", "RETURN"
    )

    # Preservar loopback
    run_cmd(
        "iptables", "-w", "-t", "nat", "-A", "TORWALL_NAT",
        "-d", "127.0.0.0/8", "-j", "RETURN"
    )

    # Redirecionar DNS UDP
    run_cmd(
        "iptables", "-w", "-t", "nat", "-A", "TORWALL_NAT",
        "-p", "udp", "--dport", "53",
        "-j", "REDIRECT", "--to-ports", str(TOR_DNS)
    )

    # Redirecionar TCP para o Tor
    run_cmd(
        "iptables", "-w", "-t", "nat", "-A", "TORWALL_NAT",
        "-p", "tcp",
        "-j", "REDIRECT", "--to-ports", str(TOR_TRANSPORT)
    )

    attach_chain("nat", "TORWALL_NAT")


def configure_iptables():
    create_chain("filter", "TORWALL_OUT")

    # Permitir loopback
    run_cmd(
        "iptables", "-w", "-t", "filter", "-A", "TORWALL_OUT",
        "-o", "lo", "-j", "ACCEPT"
    )

    # Permitir conexões originadas pelo Tor
    run_cmd(
        "iptables", "-w", "-t", "filter", "-A", "TORWALL_OUT",
        "-m", "owner", "--uid-owner", str(TOR_UID),
        "-j", "ACCEPT"
    )

    # Permitir DHCP
    run_cmd(
        "iptables", "-w", "-t", "filter", "-A", "TORWALL_OUT",
        "-p", "udp", "--sport", "68", "--dport", "67",
        "-j", "ACCEPT"
    )

    # Bloquear qualquer outro tráfego de saída
    run_cmd(
        "iptables", "-w", "-t", "filter", "-A", "TORWALL_OUT",
        "-j", "DROP"
    )

    attach_chain("filter", "TORWALL_OUT")


def configure_ip6tables():
    subprocess.run(
        ["ip6tables", "-w", "-N", "TORWALL6_OUT"],
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL
    )

    run_cmd("ip6tables", "-w", "-F", "TORWALL6_OUT")

    # Permitir loopback IPv6
    run_cmd(
        "ip6tables", "-w", "-A", "TORWALL6_OUT",
        "-o", "lo", "-j", "ACCEPT"
    )

    # Bloquear todo o restante do IPv6
    run_cmd(
        "ip6tables", "-w", "-A", "TORWALL6_OUT",
        "-j", "DROP"
    )

    result = subprocess.run(
        ["ip6tables", "-w", "-C", "OUTPUT", "-j", "TORWALL6_OUT"],
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL
    )

    if result.returncode != 0:
        run_cmd(
            "ip6tables", "-w", "-I", "OUTPUT",
            "1", "-j", "TORWALL6_OUT"
        )

def check_root():
    if os.geteuid() != 0:
        print("[ERRO] Execute como root.")
        sys.exit(1)


def run_command(command, input_text=None):
    try:
        result = subprocess.run(
            command,
            input=input_text,
            check=True,
            capture_output=True,
            text=True
        )
        return result.stdout

    except FileNotFoundError:
        print(f"[ERRO] Comando não encontrado: {command[0]}")

    except subprocess.CalledProcessError as e:
        print(f"[ERRO] {' '.join(command)}")
        print(e.stderr)

    return None


# ==========================================
# BACKUP AND RESTORE
# ==========================================

def backup_configuration():

    BACKUP_DIR.mkdir(parents=True, exist_ok=True)

    firewall_files = {
        "iptables.rules": ["iptables-save"],
        "ip6tables.rules": ["ip6tables-save"]
    }

    for filename, command in firewall_files.items():

        destination = BACKUP_DIR / filename

        if destination.exists():
            continue

        result = run_command(command)

        if result is None:
            print(f"[ERRO] Backup falhou: {filename}")
            return False

        destination.write_text(result)

    # Backup sysctl

    if not SYSCTL_BACKUP.exists():

        settings = [
            "net.ipv6.conf.all.disable_ipv6",
            "net.ipv6.conf.default.disable_ipv6",
            "net.ipv6.conf.lo.disable_ipv6"
        ]

        values = {}

        for setting in settings:

            result = run_command(["sysctl", "-n", setting])

            if result is None:
                return False

            values[setting] = result.strip()

        SYSCTL_BACKUP.write_text(
            json.dumps(values, indent=4)
        )

    # Backup torrc

    torrc_backup = BACKUP_DIR / "torrc"

    if TORRC_PATH.exists() and not torrc_backup.exists():
        shutil.copy2(TORRC_PATH, torrc_backup)
        (BACKUP_DIR / "torrc-existed").touch()

    elif not TORRC_PATH.exists():
        (BACKUP_DIR / "torrc-absent").touch()

    print("[INFO] Backup realizado.")

    return True


def restore_configuration():

    firewall_files = [
        (["iptables-restore"], BACKUP_DIR / "iptables.rules"),
        (["ip6tables-restore"], BACKUP_DIR / "ip6tables.rules")
    ]

    for command, path in firewall_files:

        if not path.exists():
            print(f"[ERRO] Backup ausente: {path}")
            return False

        result = run_command(
            command,
            input_text=path.read_text()
        )

        if result is None:
            return False

    # Restore sysctl

    if SYSCTL_BACKUP.exists():

        values = json.loads(SYSCTL_BACKUP.read_text())

        for setting, value in values.items():

            if run_command([
                "sysctl", "-w", f"{setting}={value}"
            ]) is None:
                return False

    # Restore torrc

    torrc_backup = BACKUP_DIR / "torrc"

    if torrc_backup.exists():

        TORRC_PATH.parent.mkdir(parents=True, exist_ok=True)
        shutil.copy2(torrc_backup, TORRC_PATH)

    elif (BACKUP_DIR / "torrc-absent").exists():

        if TORRC_PATH.exists():
            TORRC_PATH.unlink()

    print("[INFO] Configuração restaurada.")

    return True


# ==========================================
# FIREWALL
# ==========================================

def configure_iptables():

    print("[AVISO] Regras IPv4 implementadas.")
    return False


def configure_ip6tables():

    print("[AVISO] Regras IPv6 implementadas.")
    return False


def configure_udp():

    print("[AVISO] Política UDP implementada.")
    return False


# ==========================================
# NETWORK
# ==========================================

def disable_ipv6():

    settings = [
        "net.ipv6.conf.all.disable_ipv6=1",
        "net.ipv6.conf.default.disable_ipv6=1",
        "net.ipv6.conf.lo.disable_ipv6=1"
    ]

    for setting in settings:

        if run_command(["sysctl", "-w", setting]) is None:
            return False

    return True


def configure_dns():

    print("[AVISO] Redirecionamento DNS implementado.")
    return False


# ==========================================
# TOR
# ==========================================

def install_tor():

    if shutil.which("tor"):
        print("[INFO] Tor já instalado.")
        return True

    if run_command(["apt-get", "update"]) is None:
        return False

    result = run_command([
        "apt-get", "install", "-y", "tor"
    ])

    return result is not None


def configure_tor():

    tor_config = f"""
RunAsDaemon 1
VirtualAddrNetworkIPv4 10.192.0.0/10
AutomapHostsOnResolve 1
TransPort {TOR_TRANSPORT}
DNSPort {TOR_DNS}
"""

    TORRC_PATH.parent.mkdir(parents=True, exist_ok=True)

    TORRC_PATH.write_text(tor_config)

    print("[INFO] Tor configurado.")

    return True


def start_tor():

    if run_command(["systemctl", "enable", "tor"]) is None:
        return False

    if run_command(["systemctl", "restart", "tor"]) is None:
        return False

    return check_tor()


def stop_tor():

    return run_command([
        "systemctl", "stop", "tor"
    ]) is not None


def check_tor():

    result = run_command([
        "systemctl", "is-active", "tor"
    ])

    return result is not None and result.strip() == "active"


# ==========================================
# MAIN OPERATIONS
# ==========================================
def enable():

    check_root()

    print("[INFO] Preparando TorWall...")

    backup_configuration()

    install_tor()
    configure_tor()

    configure_nat()
    configure_iptables()
    configure_ip6tables()

    start_tor()

    print("[INFO] TorWall ativado.")
    print("[INFO] Preparando TorWall...")

    if not backup_configuration():
        print("[ERRO] Backup falhou.")
        return 1

    if not install_tor():
        print("[ERRO] Falha na instalação do Tor.")
        return 1

    if not configure_tor():
        return 1

    if not start_tor():
        print("[ERRO] Tor não iniciou.")
        return 1

    print("[INFO] Tor está ativo.")
    print("[AVISO] Firewall implementado, reporte possiveis bugs.")
    print("[AVISO] TorWall ativado.")

    return 0


def disable():

    check_root()

    if not restore_configuration():
        print("[ERRO] Falha na restauração.")
        return 1

    if not stop_tor():
        print("[ERRO] Falha ao parar Tor.")
        return 1

    print("[INFO] TorWall desativado.")

    return 0


def status():

    print("\n=== TORWALL STATUS ===")

    print(
        "Tor:",
        "ACTIVE" if check_tor() else "INACTIVE"
    )

    ipv6 = run_command([
        "sysctl", "-n",
        "net.ipv6.conf.all.disable_ipv6"
    ])

    print(
        "IPv6:",
        ipv6.strip() if ipv6 else "UNKNOWN"
    )

    firewall = run_command([
        "iptables", "-L", "-n"
    ])

    print(
        "Firewall:",
        "AVAILABLE" if firewall is not None else "ERROR"
    )

    print("Fail-closed: NOT IMPLEMENTED")
    print("Tor routing: NOT IMPLEMENTED")

    return 0


# ==========================================
# CLI
# ==========================================

def main():

    actions = {
        "install": install_tor,
        "enable": enable,
        "disable": disable,
        "status": status,
        "restore": restore_configuration
    }

    if len(sys.argv) != 2:

        print("""
TORWALL

Usage:

  install   Install Tor
  enable    Enable TorWall setup
  disable   Restore previous configuration
  status    Show system status
  restore   Restore saved configuration
""")

        return 1

    command = sys.argv[1]

    if command not in actions:
        print("[ERRO] Comando desconhecido.")
        return 1

    if command in ["enable", "disable", "restore"]:
        check_root()

    result = actions[command]()

    return result if isinstance(result, int) else 0


if __name__ == "__main__":
    sys.exit(main())
