"""Per-device TLS identity. Keys are generated on the device, never in a release image."""
import hashlib
import ipaddress
import os
from pathlib import Path
import ssl


TLS_DIRECTORY = Path('/etc/print-monitor/tls')


def ensure_identity(directory=TLS_DIRECTORY):
    from datetime import datetime, timezone
    from cryptography import x509
    from cryptography.hazmat.primitives import hashes, serialization
    from cryptography.hazmat.primitives.asymmetric import ec
    from cryptography.x509.oid import NameOID
    directory = Path(directory)
    certificate, key_file = directory / 'certificate.pem', directory / 'private-key.pem'
    directory.mkdir(parents=True, exist_ok=True, mode=0o750)
    if certificate.exists() != key_file.exists():
        raise ValueError('Incomplete TLS identity. Restore the missing identity file before restarting.')
    if not certificate.exists():
        key = ec.generate_private_key(ec.SECP256R1())
        subject = x509.Name([x509.NameAttribute(NameOID.COMMON_NAME, 'Printer Pal device')])
        cert = (x509.CertificateBuilder().subject_name(subject).issuer_name(subject)
                .public_key(key.public_key()).serial_number(x509.random_serial_number())
                # Fixed dates tolerate a Pi clock that has not synchronized on first boot.
                # The app trusts the exact certificate scanned from the local setup QR.
                .not_valid_before(datetime(1970, 1, 1, tzinfo=timezone.utc))
                .not_valid_after(datetime(2100, 1, 1, tzinfo=timezone.utc))
                .add_extension(x509.SubjectAlternativeName([x509.DNSName('localhost'),
                    x509.IPAddress(ipaddress.ip_address('127.0.0.1')),
                    x509.IPAddress(ipaddress.ip_address('::1'))]), critical=False)
                .add_extension(x509.BasicConstraints(ca=True, path_length=0), critical=True)
                .sign(key, hashes.SHA256()))
        for file, value in [(key_file, key.private_bytes(serialization.Encoding.PEM,
                serialization.PrivateFormat.PKCS8, serialization.NoEncryption())),
                (certificate, cert.public_bytes(serialization.Encoding.PEM))]:
            fd = os.open(file, os.O_CREAT | os.O_EXCL | os.O_WRONLY, 0o640)
            with os.fdopen(fd, 'wb') as output:
                output.write(value)
                output.flush()
                os.fsync(output.fileno())
    os.chmod(directory, 0o750)
    for file in (certificate, key_file):
        os.chmod(file, 0o640)
    if os.geteuid() == 0:
        import grp
        group = grp.getgrnam('print-monitor').gr_gid
        for file in (directory, certificate, key_file):
            os.chown(file, 0, group)
    return certificate, key_file


def fingerprint(certificate):
    der = ssl.PEM_cert_to_DER_cert(Path(certificate).read_text())
    return hashlib.sha256(der).hexdigest()


def server_context(directory=TLS_DIRECTORY):
    context = ssl.SSLContext(ssl.PROTOCOL_TLS_SERVER)
    context.minimum_version = ssl.TLSVersion.TLSv1_2
    context.options |= ssl.OP_NO_COMPRESSION
    context.load_cert_chain(str(Path(directory) / 'certificate.pem'), str(Path(directory) / 'private-key.pem'))
    return context


def local_client_context(directory=TLS_DIRECTORY):
    return ssl.create_default_context(cafile=str(Path(directory) / 'certificate.pem'))
