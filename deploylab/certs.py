"""A private certificate authority and one server certificate, for encrypting traffic between the services.

Open-source tooling only (the `cryptography` library). This is a lab-grade internal PKI: one CA, one
certificate valid for every service name. In production each service gets its own short-lived certificate from
a managed CA (or a service mesh does it), and the clients trust only that CA.
"""

from __future__ import annotations

import datetime as dt
import ipaddress
from pathlib import Path

from cryptography import x509
from cryptography.hazmat.primitives import hashes, serialization
from cryptography.hazmat.primitives.asymmetric import ec
from cryptography.x509.oid import ExtendedKeyUsageOID, NameOID

DEFAULT_NAMES = ("localhost", "identity", "gateway", "knowledge", "tickets")


def _name(common_name: str) -> x509.Name:
    return x509.Name([x509.NameAttribute(NameOID.ORGANIZATION_NAME, "production-deployment-lab"),
                      x509.NameAttribute(NameOID.COMMON_NAME, common_name)])


def make_certs(directory: Path, names=DEFAULT_NAMES, days: int = 30) -> dict:
    """Write ca.pem, tls_cert.pem and tls_key.pem into `directory`; return their paths."""
    directory.mkdir(parents=True, exist_ok=True)
    now = dt.datetime.now(dt.timezone.utc) - dt.timedelta(minutes=5)

    ca_key = ec.generate_private_key(ec.SECP256R1())
    ca = (x509.CertificateBuilder().subject_name(_name("lab internal CA")).issuer_name(_name("lab internal CA"))
          .public_key(ca_key.public_key()).serial_number(x509.random_serial_number())
          .not_valid_before(now).not_valid_after(now + dt.timedelta(days=days))
          .add_extension(x509.BasicConstraints(ca=True, path_length=0), critical=True)
          .add_extension(x509.SubjectKeyIdentifier.from_public_key(ca_key.public_key()), critical=False)
          .add_extension(x509.KeyUsage(digital_signature=True, key_cert_sign=True, crl_sign=True, content_commitment=False,
                                       key_encipherment=False, data_encipherment=False, key_agreement=False,
                                       encipher_only=False, decipher_only=False), critical=True)
          .sign(ca_key, hashes.SHA256()))

    key = ec.generate_private_key(ec.SECP256R1())
    sans = [x509.DNSName(n) for n in names] + [x509.IPAddress(ipaddress.ip_address("127.0.0.1"))]
    cert = (x509.CertificateBuilder().subject_name(_name(names[0])).issuer_name(ca.subject)
            .public_key(key.public_key()).serial_number(x509.random_serial_number())
            .not_valid_before(now).not_valid_after(now + dt.timedelta(days=days))
            .add_extension(x509.SubjectAlternativeName(sans), critical=False)
            .add_extension(x509.BasicConstraints(ca=False, path_length=None), critical=True)
            .add_extension(x509.AuthorityKeyIdentifier.from_issuer_public_key(ca_key.public_key()), critical=False)
            .add_extension(x509.ExtendedKeyUsage([ExtendedKeyUsageOID.SERVER_AUTH]), critical=False)
            .sign(ca_key, hashes.SHA256()))

    paths = {"ca": directory / "ca.pem", "cert": directory / "tls_cert.pem", "key": directory / "tls_key.pem"}
    paths["ca"].write_bytes(ca.public_bytes(serialization.Encoding.PEM))
    paths["cert"].write_bytes(cert.public_bytes(serialization.Encoding.PEM))
    paths["key"].write_bytes(key.private_bytes(serialization.Encoding.PEM, serialization.PrivateFormat.PKCS8,
                                               serialization.NoEncryption()))
    return paths
