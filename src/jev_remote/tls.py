from __future__ import annotations

import datetime
import ipaddress
from pathlib import Path

from cryptography import x509
from cryptography.hazmat.primitives import hashes, serialization
from cryptography.hazmat.primitives.asymmetric import ec
from cryptography.x509.oid import NameOID

DEFAULT_DIR = Path(".tls")


def _covers(cert_path: Path, names: set[str]) -> bool:
    """True if the existing certificate is valid for at least a week and lists every address."""
    try:
        cert = x509.load_pem_x509_certificate(cert_path.read_bytes())
        san = cert.extensions.get_extension_for_class(x509.SubjectAlternativeName).value
    except (OSError, ValueError, x509.ExtensionNotFound):
        return False
    have = {str(ip) for ip in san.get_values_for_type(x509.IPAddress)}
    have |= set(san.get_values_for_type(x509.DNSName))
    fresh_until = datetime.datetime.now(datetime.UTC) + datetime.timedelta(days=7)
    return names <= have and cert.not_valid_after_utc > fresh_until


def ensure_certificate(lan_address: str, directory: Path = DEFAULT_DIR) -> tuple[Path, Path]:
    """Create (or reuse) a self-signed certificate valid for this PC's LAN address."""
    directory.mkdir(parents=True, exist_ok=True)
    cert_path, key_path = directory / "cert.pem", directory / "key.pem"
    names = {"localhost", "127.0.0.1", lan_address}
    if cert_path.is_file() and key_path.is_file() and _covers(cert_path, names):
        return cert_path, key_path

    key = ec.generate_private_key(ec.SECP256R1())
    subject = x509.Name([x509.NameAttribute(NameOID.COMMON_NAME, "Jev Remote")])
    alt_names: list[x509.GeneralName] = [x509.DNSName("localhost")]
    for address in ("127.0.0.1", lan_address):
        alt_names.append(x509.IPAddress(ipaddress.ip_address(address)))
    now = datetime.datetime.now(datetime.UTC)
    cert = (
        x509.CertificateBuilder()
        .subject_name(subject)
        .issuer_name(subject)
        .public_key(key.public_key())
        .serial_number(x509.random_serial_number())
        .not_valid_before(now - datetime.timedelta(days=1))
        .not_valid_after(now + datetime.timedelta(days=825))
        .add_extension(x509.SubjectAlternativeName(alt_names), critical=False)
        .add_extension(x509.BasicConstraints(ca=False, path_length=None), critical=True)
        .sign(key, hashes.SHA256())
    )
    cert_path.write_bytes(cert.public_bytes(serialization.Encoding.PEM))
    key_path.write_bytes(
        key.private_bytes(
            serialization.Encoding.PEM,
            serialization.PrivateFormat.PKCS8,
            serialization.NoEncryption(),
        )
    )
    return cert_path, key_path
