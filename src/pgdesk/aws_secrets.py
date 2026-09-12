"""Resolve explicitly configured AWS secrets without exposing their values in diagnostics."""

from __future__ import annotations

import json
import os
import re
import subprocess
from dataclasses import dataclass
from typing import Any


@dataclass(frozen=True)
class AwsSecret:
    """Non-secret identity of a JSON secret; AWS CLI owns profile authentication and refresh."""

    profile: str
    secret_id: str
    region: str

    @classmethod
    def from_config(cls, value: object) -> AwsSecret:
        """Reject incomplete or ambiguous references before any AWS request is made."""
        if not isinstance(value, dict) or set(value) != {"profile", "secret_id", "region"}:
            raise ValueError("aws_secret requires exactly profile, secret_id and region")
        if any(not isinstance(item, str) or not item.strip() for item in value.values()):
            raise ValueError("AWS profile, secret_id and region must be nonempty strings")
        return cls(**value)

    def read(self) -> dict[str, Any]:
        """Fetch current JSON in memory, suppressing raw AWS responses on every failure path."""
        try:
            result = subprocess.run(
                [
                    "aws",
                    "secretsmanager",
                    "get-secret-value",
                    "--profile",
                    self.profile,
                    "--region",
                    self.region,
                    "--secret-id",
                    self.secret_id,
                    "--query",
                    "SecretString",
                    "--output",
                    "text",
                    "--no-cli-pager",
                    "--cli-connect-timeout",
                    "5",
                    "--cli-read-timeout",
                    "10",
                ],
                env=dict(os.environ, AWS_PROFILE=self.profile, AWS_PAGER="", AWS_MAX_ATTEMPTS="2"),
                capture_output=True,
                text=True,
                timeout=25,
                check=False,
            )
        except FileNotFoundError:
            raise ValueError(
                "Install AWS CLI to use configured Secrets Manager credentials"
            ) from None
        except subprocess.TimeoutExpired:
            raise ValueError(f"AWS secret request timed out for profile {self.profile}") from None
        if result.returncode:
            match = re.search(r"An error occurred \(([A-Za-z0-9]+)\)", result.stderr)
            code = match.group(1) if match else "authentication or connection failed"
            raise ValueError(
                f"AWS Secrets Manager: {code} (profile {self.profile}); check AWS login and secret access"
            )
        try:
            value = json.loads(result.stdout)
        except (ValueError, TypeError):
            raise ValueError("AWS secret must contain a JSON object; response suppressed") from None
        if not isinstance(value, dict):
            raise ValueError("AWS secret must contain a JSON object; response suppressed")
        return value

    def text(self, path: tuple[str, ...]) -> str:
        """Read a required nested string without including missing or invalid secret values."""
        value: Any = self.read()
        for part in path:
            if not isinstance(value, dict) or part not in value:
                raise ValueError("Configured key path is missing from the AWS secret")
            value = value[part]
        if not isinstance(value, str) or not value.strip():
            raise ValueError("Configured AWS secret key must be a nonempty string")
        return value

    def postgres_kwargs(self) -> dict[str, Any]:
        """Map the standard RDS secret format to libpq, with verified TLS and no stale cache."""
        value = self.read()
        if any(
            not isinstance(value.get(key), str) or not value[key]
            for key in ("host", "username", "password")
        ):
            raise ValueError("RDS secret requires nonempty host, username and password fields")
        try:
            port = int(value["port"])
        except (KeyError, TypeError, ValueError, OverflowError):
            raise ValueError("RDS secret requires a valid TCP port") from None
        if isinstance(value["port"], bool) or not 1 <= port <= 65535:
            raise ValueError("RDS secret requires a valid TCP port")
        return {
            "host": value["host"],
            "port": port,
            "user": value["username"],
            "password": value["password"],
            "sslmode": "verify-full",
        }
