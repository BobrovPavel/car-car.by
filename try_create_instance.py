"""
try_create_instance.py
Ловушка для Oracle Cloud Always Free A1.Flex.
Каждые TRY_EVERY секунд пытается создать инстанс. При успехе — пишет в лог
и поднимает шум: звуковой сигнал + создание файла-маркера SUCCESS.txt
с публичным IP машины.

Запуск:
    python try_create_instance.py

Прерывание:
    Ctrl+C

Безопасно прерывать. При перезапуске ничего не дублирует — если capacity
есть и инстанс создался один раз, скрипт остановится сам.
"""

import json
import sys
import time
from datetime import datetime
from pathlib import Path

import oci


# ===== параметры =====
COMPARTMENT_ID = "ocid1.tenancy.oc1..aaaaaaaaqtkwrdxebshq26zazdqpu3zzxr6bdlewdvibghksej4tsfunx24a"
SUBNET_ID      = "ocid1.subnet.oc1.eu-stockholm-1.aaaaaaaaoi4psfwvacu4bo36pjfj3uifgnom4fspwubx4b3chekhd5hkjo4q"
IMAGE_ID       = "ocid1.image.oc1.eu-stockholm-1.aaaaaaaazir62xrvbzdlkuxaocszd5vearz3g5lvepuu3wer6jcderozo65q"
AD             = "SAUQ:EU-STOCKHOLM-1-AD-1"

SHAPE          = "VM.Standard.A1.Flex"
OCPUS          = 4         # пробуем сразу взять максимум Always Free
MEMORY_GB      = 24
BOOT_VOLUME_GB = 100
INSTANCE_NAME  = "car-car"

# путь к public SSH-ключу (для доступа на машину после создания)
SSH_PUBLIC_KEY = Path.home() / ".ssh" / "car-car.key.pub"

# как часто пытаться (секунд). 5 минут = 300 — рекомендую не уменьшать,
# иначе Oracle может посчитать abuse и забанить tenancy.
TRY_EVERY = 300

# fallback: если N попыток подряд "out of capacity" — уменьшаем shape,
# чтобы попробовать с меньшими ресурсами. 0 = не уменьшать.
DOWNGRADE_AFTER = 0


# ===== код =====

def log(msg):
    ts = datetime.now().strftime("%H:%M:%S")
    print(f"[{ts}] {msg}", flush=True)


def load_ssh_key() -> str:
    if not SSH_PUBLIC_KEY.exists():
        log(f"ОШИБКА: не найден SSH public key по пути {SSH_PUBLIC_KEY}")
        log("Скрипт остановится. Проверь путь к публичному ключу.")
        sys.exit(1)
    return SSH_PUBLIC_KEY.read_text().strip()


def try_launch(client, ocpus, memory_gb, ssh_key) -> tuple[bool, str]:
    """
    Одна попытка создать инстанс. Возвращает (success, message).
    success=True — инстанс создан, message содержит IP.
    success=False — message содержит причину ('OUT_OF_CAPACITY', 'LIMIT_EXCEEDED' и т.п.).
    """
    details = oci.core.models.LaunchInstanceDetails(
        availability_domain=AD,
        compartment_id=COMPARTMENT_ID,
        display_name=INSTANCE_NAME,
        shape=SHAPE,
        shape_config=oci.core.models.LaunchInstanceShapeConfigDetails(
            ocpus=ocpus,
            memory_in_gbs=memory_gb,
        ),
        source_details=oci.core.models.InstanceSourceViaImageDetails(
            source_type="image",
            image_id=IMAGE_ID,
            boot_volume_size_in_gbs=BOOT_VOLUME_GB,
        ),
        create_vnic_details=oci.core.models.CreateVnicDetails(
            subnet_id=SUBNET_ID,
            assign_public_ip=True,
        ),
        metadata={
            "ssh_authorized_keys": ssh_key,
        },
    )

    try:
        resp = client.launch_instance(details)
        instance = resp.data
        log(f"✓ ИНСТАНС СОЗДАН: id={instance.id}")
        return True, instance.id

    except oci.exceptions.ServiceError as e:
        # Out of capacity — ожидаемое, его и ловим
        if e.status == 500 and "Out of host capacity" in (e.message or ""):
            return False, "OUT_OF_CAPACITY"
        if e.status == 400 and "LimitExceeded" in (e.code or ""):
            return False, f"LIMIT_EXCEEDED: {e.message}"
        # любое другое — лог и пробрасываем
        log(f"✗ Service error {e.status} {e.code}: {e.message}")
        return False, f"ERROR: {e.code}"

    except Exception as e:
        log(f"✗ Неожиданная ошибка: {e}")
        return False, f"ERROR: {e}"


def wait_for_running_and_get_ip(client, network_client, instance_id) -> str | None:
    """После создания ждём пока инстанс перейдёт в RUNNING и возвращаем public IP."""
    log("жду RUNNING и получаю IP...")
    for _ in range(60):  # до 5 минут
        instance = client.get_instance(instance_id).data
        if instance.lifecycle_state == "RUNNING":
            break
        time.sleep(5)
    else:
        log("инстанс не пришёл в RUNNING за 5 минут (но создан)")
        return None

    # достаём VNIC чтобы вытащить публичный IP
    vnics = client.list_vnic_attachments(
        compartment_id=COMPARTMENT_ID, instance_id=instance_id
    ).data
    if not vnics:
        return None
    vnic = network_client.get_vnic(vnics[0].vnic_id).data
    return vnic.public_ip


def announce_success(ip: str, instance_id: str):
    """Поднимаем шум: файл-маркер, beep, сообщение."""
    marker = Path.cwd() / "SUCCESS.txt"
    marker.write_text(
        f"Oracle инстанс создан!\n"
        f"IP:          {ip}\n"
        f"Instance ID: {instance_id}\n"
        f"Время:       {datetime.now().isoformat()}\n"
        f"\n"
        f"Подключиться:\n"
        f"  ssh -i C:\\Users\\raind\\.ssh\\car-car.key ubuntu@{ip}\n",
        encoding="utf-8",
    )
    log(f"маркер сохранён в {marker}")

    # звуковой сигнал
    try:
        import winsound
        for _ in range(5):
            winsound.Beep(1500, 300)
            time.sleep(0.2)
    except Exception:
        print("\a" * 5, flush=True)


def main():
    log("=" * 60)
    log("Oracle Cloud capacity hunter")
    log(f"shape:       {SHAPE} ({OCPUS} OCPU, {MEMORY_GB} GB)")
    log(f"region:      eu-stockholm-1, AD: {AD}")
    log(f"interval:    {TRY_EVERY}с")
    log("=" * 60)

    ssh_key = load_ssh_key()

    config = oci.config.from_file()
    compute = oci.core.ComputeClient(config)
    network = oci.core.VirtualNetworkClient(config)

    attempt = 0
    ocpus, memory_gb = OCPUS, MEMORY_GB
    capacity_fails = 0

    while True:
        attempt += 1
        log(f"попытка #{attempt}: ocpus={ocpus}, memory={memory_gb}GB")

        success, msg = try_launch(compute, ocpus, memory_gb, ssh_key)

        if success:
            instance_id = msg
            ip = wait_for_running_and_get_ip(compute, network, instance_id)
            log("=" * 60)
            log(f"УСПЕХ! Public IP: {ip}")
            log("=" * 60)
            announce_success(ip or "не получен", instance_id)
            return

        if msg == "OUT_OF_CAPACITY":
            capacity_fails += 1
            log(f"  out of capacity (всего подряд: {capacity_fails})")

            if DOWNGRADE_AFTER and capacity_fails >= DOWNGRADE_AFTER:
                if ocpus > 1:
                    ocpus = max(1, ocpus // 2)
                    memory_gb = max(6, memory_gb // 2)
                    log(f"  понижаю shape до {ocpus}/{memory_gb}GB")
                    capacity_fails = 0
        else:
            log(f"  {msg}")
            if "LIMIT_EXCEEDED" in msg:
                log("у тебя уже создан инстанс на лимите Always Free — проверь Console")
                return

        log(f"  жду {TRY_EVERY}с до следующей попытки...")
        time.sleep(TRY_EVERY)


if __name__ == "__main__":
    try:
        main()
    except KeyboardInterrupt:
        log("остановлено пользователем")