from cryptography import x509

from jev_remote.tls import ensure_certificate


def test_certificate_covers_lan_address_and_is_reused(tmp_path):
    cert, key = ensure_certificate("192.168.1.50", tmp_path)
    assert cert.is_file() and key.is_file()
    san = x509.load_pem_x509_certificate(cert.read_bytes()).extensions.get_extension_for_class(
        x509.SubjectAlternativeName
    ).value
    assert "192.168.1.50" in {str(ip) for ip in san.get_values_for_type(x509.IPAddress)}

    before = cert.read_bytes()
    ensure_certificate("192.168.1.50", tmp_path)
    assert cert.read_bytes() == before


def test_certificate_regenerated_when_address_changes(tmp_path):
    cert, _ = ensure_certificate("192.168.1.50", tmp_path)
    before = cert.read_bytes()
    ensure_certificate("192.168.1.99", tmp_path)
    assert cert.read_bytes() != before
