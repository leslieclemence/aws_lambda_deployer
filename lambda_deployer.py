#!/usr/bin/env python3
"""
camedics_lambda_deployer/lambda_deployer.py
============================================
Interactive AWS Lambda deployer powered by AWS SAM CLI.

Workflow
--------
1. Select an AWS CLI profile (or use environment defaults).
2. Collect / confirm Lambda configuration:
   name, handler, runtime, memory, timeout, IAM role, env vars, layers, Function URL.
3. Generate ``template.yaml`` and ``samconfig.toml``.
4. Run ``sam build`` then ``sam deploy``.
5. Optionally wire the Function URL to a custom domain via CloudFront + Route 53.

Configuration is persisted to ``.lambda_deployer_config.json`` so subsequent
runs can reuse all settings without re-entering every field.

Usage
-----
::

    cd /path/to/my-lambda-project
    python lambda_deployer.py

Dependencies
------------
* **boto3** – AWS SDK.
* **PyYAML** – YAML serialisation for ``template.yaml``.
* **aws-sam-cli** – External CLI (``sam``); install with ``brew install aws-sam-cli``.
"""

from __future__ import annotations

import argparse
import ast
import configparser
import json
import os
import re
import socket
import subprocess
import sys
import time
import traceback
import urllib.error
import urllib.request
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Callable, Dict, List, Optional, Tuple
from urllib.parse import urlparse

import boto3
import yaml
from botocore.exceptions import ClientError

__all__ = ["LambdaDeployer", "main"]

# ---------------------------------------------------------------------------
# Module-level constants
# ---------------------------------------------------------------------------

#: CloudFront's fixed hosted zone ID (used for Route 53 alias records).
CLOUDFRONT_HOSTED_ZONE_ID = "Z2FDTNDATAQYW2"

#: Managed policy ID that disables CloudFront caching (pass-through).
CLOUDFRONT_CACHE_POLICY_DISABLED = "4135ea2d-6df8-44a3-9df3-4b5a84be39ad"

#: Origin request policy that forwards all viewer headers except Host.
CLOUDFRONT_ORIGIN_REQUEST_ALL_VIEWER_EXCEPT_HOST = "b689b0a8-53d0-40ab-baf2-68738e2966ac"

#: Seconds to wait after creating an IAM role so it propagates globally.
IAM_ROLE_PROPAGATION_WAIT = 10

DEFAULT_RUNTIME = "python3.13"
DEFAULT_MEMORY_MB = 128
DEFAULT_TIMEOUT_S = 30

#: Trust policy document granting Lambda the sts:AssumeRole permission.
_LAMBDA_TRUST_POLICY: Dict[str, Any] = {
    "Version": "2012-10-17",
    "Statement": [
        {
            "Effect": "Allow",
            "Principal": {"Service": "lambda.amazonaws.com"},
            "Action": "sts:AssumeRole",
        }
    ],
}

_LAMBDA_BASIC_EXECUTION_ARN = (
    "arn:aws:iam::aws:policy/service-role/AWSLambdaBasicExecutionRole"
)


# ---------------------------------------------------------------------------
# Generic polling helper
# ---------------------------------------------------------------------------

def _poll_until(
    condition: Callable[[], Optional[Any]],
    description: str,
    max_wait: int,
    interval: int = 10,
    *,
    silent_first: bool = True,
) -> Optional[Any]:
    """Poll *condition* until it returns a truthy value or *max_wait* seconds elapse.

    Parameters
    ----------
    condition:
        Zero-argument callable.  A truthy return value signals success.
    description:
        Human-readable label used in progress/timeout messages.
    max_wait:
        Maximum total wait time in seconds.
    interval:
        Seconds between successive calls to *condition*.
    silent_first:
        When ``True`` the first call is made before printing any progress line.

    Returns
    -------
    The first truthy value returned by *condition*, or ``None`` on timeout.
    """
    elapsed = 0
    first = True
    while elapsed <= max_wait:
        result = condition()
        if result:
            return result
        if not (first and silent_first):
            print(f"  Aguardando {description}... {elapsed}s/{max_wait}s")
        first = False
        time.sleep(interval)
        elapsed += interval
    print(f"⚠ Timeout aguardando {description}.")
    return None


# ---------------------------------------------------------------------------
# Shared user-interaction helpers
# ---------------------------------------------------------------------------

def _ask(
    prompt: str,
    default: Optional[str] = None,
    validate: Optional[Callable[[str], Tuple[bool, str]]] = None,
) -> str:
    """Prompt for text input with an optional default value and validator.

    Loops until the user enters a valid, non-empty answer.

    Parameters
    ----------
    prompt:
        Question text displayed to the user (without trailing colon).
    default:
        Value shown in brackets and used when the user presses Enter.
        Pass ``None`` to make the field mandatory (no default).
    validate:
        Callable ``(value) -> (is_valid, error_message)``.  Only called when
        *value* is non-empty.
    """
    while True:
        display = f"{prompt} [{default}]: " if default is not None else f"{prompt}: "
        raw = input(display).strip()
        value = raw if raw else (default if default is not None else "")

        if not value:
            print("⚠ Este campo é obrigatório!")
            continue

        if validate:
            ok, error = validate(value)
            if not ok:
                print(f"⚠ {error}")
                continue

        return value


def _ask_yes_no(prompt: str, default: bool = False) -> bool:
    """Ask a yes/no question and return a boolean."""
    hint = "S/n" if default else "s/N"
    raw = input(f"{prompt} [{hint}]: ").strip().lower()
    return default if not raw else raw in ("s", "sim", "y", "yes")


# ---------------------------------------------------------------------------
# Validation functions
# ---------------------------------------------------------------------------

def _validate_memory(value: str) -> Tuple[bool, str]:
    """Validate Lambda memory: 128–10240 MB, multiples of 64."""
    try:
        mem = int(value)
    except ValueError:
        return False, "Memória deve ser um número inteiro"
    if not (128 <= mem <= 10240):
        return False, "Memória deve estar entre 128 e 10240 MB"
    if mem % 64 != 0:
        return False, "Memória deve ser múltiplo de 64 MB"
    return True, ""


def _validate_timeout(value: str) -> Tuple[bool, str]:
    """Validate Lambda timeout: 1–900 seconds."""
    try:
        t = int(value)
    except ValueError:
        return False, "Timeout deve ser um número inteiro"
    if not (1 <= t <= 900):
        return False, "Timeout deve estar entre 1 e 900 segundos"
    return True, ""


def _validate_stack_name(name: str) -> Tuple[bool, str]:
    """Validate CloudFormation stack name per AWS rules.

    Rules: 1–128 chars, starts with a letter, only letters/digits/hyphens,
    no trailing hyphen, no consecutive hyphens.
    """
    if not name:
        return False, "Nome da stack é obrigatório"
    if len(name) > 128:
        return False, "Nome da stack deve ter no máximo 128 caracteres"
    if not name[0].isalpha():
        return False, "Nome da stack deve começar com uma letra"
    if name.endswith("-"):
        return False, "Nome da stack não pode terminar com hífen"
    if "--" in name:
        return False, "Nome da stack não pode ter hífens consecutivos"
    if not re.match(r"^[a-zA-Z][a-zA-Z0-9-]*$", name):
        return False, "Nome da stack: apenas letras, números e hífens (sem underscore)"
    return True, ""


def _validate_subdomain(subdomain: str) -> Tuple[bool, str]:
    """Validate a DNS subdomain (e.g. ``api`` or ``api.staging``)."""
    if subdomain in ("@", ""):
        return True, ""
    for label in subdomain.split("."):
        if not label:
            return False, "Subdomínio não pode ter labels vazios"
        if len(label) > 63:
            return False, "Cada parte do subdomínio deve ter no máximo 63 caracteres"
        if not re.match(r"^[A-Za-z0-9](?:[A-Za-z0-9-]{0,61}[A-Za-z0-9])?$", label):
            return False, "Use apenas letras, números e hífens; não comece/termine com hífen"
    return True, ""


def _validate_path_prefix(value: str) -> Tuple[bool, str]:
    """Validate a CloudFront path prefix (e.g. ``/api``)."""
    path = value.strip()
    if path and path != "/":
        path = "/" + path.strip("/")
    if any(c in path for c in ("?", "#", "\\")) or re.search(r"\s", path):
        return False, "Informe apenas o caminho, sem espaços, query string ou fragmento"
    if len(path) > 255:
        return False, "Caminho muito longo para usar como behavior do CloudFront"
    return True, ""


# ---------------------------------------------------------------------------
# AWS session / client management
# ---------------------------------------------------------------------------

class AWSClientManager:
    """Lazy-initialised AWS service client factory.

    All service clients are created on first access and cached for reuse.
    This removes the need for ``Optional`` client attributes scattered across
    the codebase and the ``if not self.xxx_client`` guards they require.

    Parameters
    ----------
    profile:
        AWS CLI profile name.  ``None`` uses the default credential chain.
    region:
        AWS region.  Falls back to the profile's configured region or
        ``sa-east-1`` as a last resort.
    """

    _FALLBACK_REGION = "sa-east-1"

    def __init__(
        self,
        profile: Optional[str] = None,
        region: Optional[str] = None,
    ) -> None:
        self.profile = profile
        self.region = region
        self._session: Optional[boto3.Session] = None
        self._clients: Dict[str, Any] = {}

    # ── session ────────────────────────────────────────────────────────────

    @property
    def session(self) -> boto3.Session:
        """Return a cached :class:`boto3.Session`."""
        if self._session is None:
            self._session = (
                boto3.Session(profile_name=self.profile)
                if self.profile
                else boto3.Session()
            )
            if not self.region:
                self.region = (
                    self._session.region_name
                    or os.environ.get("AWS_REGION")
                    or os.environ.get("AWS_DEFAULT_REGION")
                    or self._FALLBACK_REGION
                )
        return self._session

    # ── generic client accessor ────────────────────────────────────────────

    def client(self, service: str, *, region: Optional[str] = None) -> Any:
        """Return a cached boto3 client for *service*.

        Parameters
        ----------
        service:
            AWS service identifier (e.g. ``"lambda"``, ``"iam"``).
        region:
            Override the default region for this specific client.
        """
        key = f"{service}:{region or self.region}"
        if key not in self._clients:
            self._clients[key] = self.session.client(
                service, region_name=region or self.region
            )
        return self._clients[key]

    # ── typed convenience properties ───────────────────────────────────────

    @property
    def lambda_(self) -> Any:
        return self.client("lambda")

    @property
    def iam(self) -> Any:
        return self.client("iam")

    @property
    def route53(self) -> Any:
        # Route 53 is a global service; region is irrelevant but boto3 needs one.
        return self.client("route53")

    @property
    def cloudfront(self) -> Any:
        return self.client("cloudfront")

    @property
    def acm_us_east_1(self) -> Any:
        """ACM client pinned to ``us-east-1`` (required for CloudFront certificates)."""
        return self.client("acm", region="us-east-1")

    # ── env helper ─────────────────────────────────────────────────────────

    def get_env(self) -> Dict[str, str]:
        """Return a copy of ``os.environ`` with AWS credential vars injected."""
        env = os.environ.copy()
        if self.profile:
            env["AWS_PROFILE"] = self.profile
            env.setdefault("AWS_SDK_LOAD_CONFIG", "1")
        if self.region:
            env.setdefault("AWS_DEFAULT_REGION", self.region)
        return env


# ---------------------------------------------------------------------------
# AWS profile selection
# ---------------------------------------------------------------------------

class ProfileSelector:
    """Discovers and interactively selects an AWS CLI profile.

    Profile names are read from ``aws configure list-profiles`` (preferred)
    or parsed directly from ``~/.aws/credentials`` and ``~/.aws/config``.
    """

    @staticmethod
    def load_profiles() -> List[str]:
        """Return all unique AWS profile names found on this machine."""
        profiles: List[str] = []
        try:
            result = subprocess.run(
                ["aws", "configure", "list-profiles"],
                capture_output=True, text=True, check=False,
            )
            if result.returncode == 0:
                profiles.extend(
                    line.strip()
                    for line in result.stdout.splitlines()
                    if line.strip()
                )
        except FileNotFoundError:
            pass

        if not profiles:
            aws_dir = Path.home() / ".aws"
            profiles.extend(ProfileSelector._parse_file(aws_dir / "credentials"))
            profiles.extend(
                ProfileSelector._parse_file(aws_dir / "config", is_config=True)
            )

        return ProfileSelector._dedupe(profiles)

    @staticmethod
    def _parse_file(path: Path, *, is_config: bool = False) -> List[str]:
        """Parse profile names from an AWS credentials or config file."""
        if not path.exists():
            return []
        parser = configparser.RawConfigParser()
        try:
            parser.read(path)
        except configparser.Error:
            return []
        names: List[str] = []
        for section in parser.sections():
            if section == "default":
                names.append("default")
            elif is_config and section.startswith("profile "):
                names.append(section.removeprefix("profile ").strip())
            elif not is_config:
                names.append(section)
        return names

    @staticmethod
    def _dedupe(seq: List[str]) -> List[str]:
        """Remove duplicates while preserving insertion order."""
        seen: set = set()
        return [x for x in seq if x and not (x in seen or seen.add(x))]  # type: ignore[func-returns-value]

    @staticmethod
    def get_profile_region(profile: str) -> Optional[str]:
        """Return the default region configured for *profile*, or ``None``."""
        try:
            result = subprocess.run(
                ["aws", "configure", "get", "region", "--profile", profile],
                capture_output=True, text=True, check=False,
            )
            if result.returncode == 0 and result.stdout.strip():
                return result.stdout.strip()
        except FileNotFoundError:
            pass
        try:
            return boto3.Session(profile_name=profile).region_name
        except Exception:
            return None

    def select(
        self, saved_profile: Optional[str] = None
    ) -> Tuple[Optional[str], Optional[str]]:
        """Interactively ask the user to choose an AWS profile.

        Returns
        -------
        ``(profile_name, region)`` — either element may be ``None``.
        """
        profiles = self.load_profiles()
        env_profile = os.environ.get("AWS_PROFILE")

        if not profiles:
            print("\n⚠ Nenhum perfil AWS encontrado em ~/.aws ou via AWS CLI")
            if env_profile:
                region = self.get_profile_region(env_profile)
                print(f"✓ Usando AWS_PROFILE do ambiente: {env_profile}")
                return env_profile, region
            print("  Continuando com a cadeia padrão de credenciais da AWS")
            return None, None

        # Pick a sensible default: saved → env var → "default" → first in list.
        default = next(
            (p for p in [saved_profile, env_profile, "default"] if p in profiles),
            profiles[0],
        )

        self._display(profiles)

        if saved_profile and saved_profile not in profiles:
            print(f"⚠ Perfil salvo '{saved_profile}' não foi encontrado. Escolha outro.")

        while True:
            raw = input(f"Digite o número ou nome do perfil AWS [{default}]: ").strip()
            profile = default if not raw else self._resolve(profiles, raw)

            if not profile:
                print("⚠ Perfil não informado!")
                continue
            if profile not in profiles:
                print(f"⚠ Perfil '{profile}' não encontrado.")
                continue

            region = self.get_profile_region(profile)
            os.environ["AWS_PROFILE"] = profile
            os.environ.setdefault("AWS_SDK_LOAD_CONFIG", "1")
            if region:
                os.environ.setdefault("AWS_DEFAULT_REGION", region)

            print(f"✓ Usando perfil AWS: {profile}")
            if region:
                print(f"✓ Região do perfil: {region}")
            return profile, region

    @staticmethod
    def _display(profiles: List[str]) -> None:
        print("\n🔐 Perfis AWS disponíveis:")
        print("-" * 60)
        for i, p in enumerate(profiles, 1):
            region = ProfileSelector.get_profile_region(p) or "(sem região)"
            print(f"  {i:2d}. {p:<25} [{region}]")
        print("-" * 60)

    @staticmethod
    def _resolve(profiles: List[str], raw: str) -> str:
        """Convert a numeric index or literal name to a profile name."""
        if raw.isdigit():
            idx = int(raw) - 1
            if 0 <= idx < len(profiles):
                return profiles[idx]
        return raw


# ---------------------------------------------------------------------------
# Persistent configuration store
# ---------------------------------------------------------------------------

class ConfigStore:
    """Reads and writes the deployer's JSON configuration file.

    Parameters
    ----------
    path:
        Path to the JSON file (e.g. ``.lambda_deployer_config.json``).
    """

    def __init__(self, path: Path) -> None:
        self.path = path

    def load(self) -> Optional[Dict[str, Any]]:
        """Load config from disk.  Returns ``None`` if the file is absent or corrupt."""
        if not self.path.exists():
            return None
        try:
            return json.loads(self.path.read_text(encoding="utf-8"))
        except (json.JSONDecodeError, OSError) as exc:
            print(f"⚠ Erro ao carregar configurações: {exc}")
            return None

    def save(self, config: Dict[str, Any]) -> None:
        """Write *config* to disk as pretty-printed JSON."""
        try:
            self.path.write_text(
                json.dumps(config, indent=2, ensure_ascii=False), encoding="utf-8"
            )
            print(f"✓ Configurações salvas em {self.path.name}")
        except OSError as exc:
            print(f"⚠ Erro ao salvar configurações: {exc}")


# ---------------------------------------------------------------------------
# IAM role management
# ---------------------------------------------------------------------------

class IAMManager:
    """Creates and queries IAM execution roles for Lambda functions.

    Parameters
    ----------
    aws:
        Shared :class:`AWSClientManager` instance.
    """

    def __init__(self, aws: AWSClientManager) -> None:
        self._aws = aws

    def get_role_arn(self, role_name: str) -> Optional[str]:
        """Return the ARN of *role_name*, or ``None`` if it does not exist."""
        try:
            return self._aws.iam.get_role(RoleName=role_name)["Role"]["Arn"]
        except ClientError as exc:
            if exc.response["Error"]["Code"] == "NoSuchEntity":
                return None
            print(f"⚠ Erro ao buscar role: {exc}")
            return None

    def list_lambda_roles(self) -> List[Dict[str, str]]:
        """Return all IAM roles whose trust policy allows Lambda to assume them."""
        roles: List[Dict[str, str]] = []
        try:
            paginator = self._aws.iam.get_paginator("list_roles")
            for page in paginator.paginate():
                for role in page["Roles"]:
                    for stmt in role["AssumeRolePolicyDocument"].get("Statement", []):
                        if "lambda.amazonaws.com" in str(stmt.get("Principal", {})):
                            roles.append({
                                "name": role["RoleName"],
                                "arn": role["Arn"],
                                "description": role.get("Description", "Sem descrição"),
                            })
                            break
        except ClientError as exc:
            print(f"⚠ Erro ao listar roles: {exc}")
        return roles

    def create_role(
        self,
        role_name: str,
        description: str,
        inline_policy: Optional[Dict[str, Any]] = None,
    ) -> Optional[str]:
        """Create an IAM execution role for Lambda.

        Always attaches ``AWSLambdaBasicExecutionRole``.  If *inline_policy*
        is provided it is attached as an inline policy named
        ``{role_name}-policy``.

        Parameters
        ----------
        role_name:
            Desired IAM role name.
        description:
            Short text stored in the role's Description field.
        inline_policy:
            Optional IAM policy document dict (permissions statements only —
            the trust policy is added automatically).

        Returns
        -------
        Role ARN on success, ``None`` on failure.
        """
        print(f"\n🔧 Criando role IAM: {role_name}")
        try:
            arn: str = self._aws.iam.create_role(
                RoleName=role_name,
                AssumeRolePolicyDocument=json.dumps(_LAMBDA_TRUST_POLICY),
                Description=description,
            )["Role"]["Arn"]
        except ClientError as exc:
            if exc.response["Error"]["Code"] == "EntityAlreadyExists":
                print(f"✓ Role {role_name} já existe")
                return self.get_role_arn(role_name)
            print(f"✗ Erro ao criar role: {exc}")
            return None

        print("  Anexando política AWSLambdaBasicExecutionRole...")
        self._aws.iam.attach_role_policy(
            RoleName=role_name, PolicyArn=_LAMBDA_BASIC_EXECUTION_ARN
        )

        if inline_policy:
            policy_name = f"{role_name}-policy"
            print(f"  Criando política inline: {policy_name}...")
            self._aws.iam.put_role_policy(
                RoleName=role_name,
                PolicyName=policy_name,
                PolicyDocument=json.dumps(inline_policy),
            )

        print(f"✓ Role criada: {arn}")
        print(f"  💡 Aguardando {IAM_ROLE_PROPAGATION_WAIT}s para propagação...")
        time.sleep(IAM_ROLE_PROPAGATION_WAIT)
        return arn


# ---------------------------------------------------------------------------
# SAM CLI runner
# ---------------------------------------------------------------------------

class SAMRunner:
    """Wraps ``sam build``, ``sam deploy``, and ``sam delete`` CLI commands.

    Parameters
    ----------
    project_root:
        Directory where ``template.yaml`` and ``samconfig.toml`` live.
    aws:
        Used to inject AWS profile/region environment variables.
    """

    # CloudFormation stack states that indicate a failed previous deploy.
    _ROLLBACK_STATES = frozenset({
        "UPDATE_ROLLBACK_IN_PROGRESS",
        "UPDATE_ROLLBACK_COMPLETE",
        "ROLLBACK_IN_PROGRESS",
        "ROLLBACK_COMPLETE",
        "UPDATE_ROLLBACK_FAILED",
    })

    def __init__(self, project_root: Path, aws: AWSClientManager) -> None:
        self.project_root = project_root
        self._aws = aws

    def is_installed(self) -> bool:
        """Return ``True`` if the ``sam`` binary is reachable on ``PATH``."""
        try:
            result = subprocess.run(
                ["sam", "--version"], capture_output=True, text=True, check=False
            )
            if result.returncode == 0:
                print(f"✓ SAM CLI encontrado: {result.stdout.strip()}")
                return True
        except FileNotFoundError:
            pass
        return False

    def build(self) -> bool:
        """Run ``sam build``.  Returns ``True`` on success."""
        print("\n" + "=" * 60)
        print("🔨 EXECUTANDO SAM BUILD")
        print("=" * 60)
        try:
            subprocess.run(
                ["sam", "build"],
                cwd=self.project_root,
                env=self._aws.get_env(),
                check=True,
            )
            print("\n✓ Build concluído com sucesso!")
            return True
        except subprocess.CalledProcessError as exc:
            print(f"\n✗ Erro no build: {exc}")
            return False
        except FileNotFoundError:
            print("\n✗ SAM CLI não encontrado!")
            return False

    def deploy(self, guided: bool = False, *, _retried: bool = False) -> bool:
        """Run ``sam deploy [--guided]``.

        Returns ``True`` on success *or* when there are no changes to deploy.
        On a rollback state, offers to delete the stack and retry once.
        Output is streamed in real-time; captured in memory to detect error patterns.
        """
        print("\n" + "=" * 60)
        print("🚀 EXECUTANDO SAM DEPLOY")
        print("=" * 60)
        cmd = ["sam", "deploy"] + (["--guided"] if guided else [])
        try:
            proc = subprocess.Popen(
                cmd,
                cwd=self.project_root,
                env=self._aws.get_env(),
                stdout=subprocess.PIPE,
                stderr=subprocess.STDOUT,
                text=True,
            )
            captured_lines: List[str] = []
            assert proc.stdout is not None
            for line in proc.stdout:
                print(line, end="", flush=True)
                captured_lines.append(line)
            proc.wait()

            output = "".join(captured_lines)

            if proc.returncode == 0:
                print("\n✓ Deploy concluído com sucesso!")
                return True

            if "No changes to deploy" in output:
                print("\n💡 Stack já está atualizada — nenhuma mudança detectada")
                return True

            if any(s in output for s in self._ROLLBACK_STATES):
                print("\n⚠ A stack está em estado de erro/rollback")
                print("  Isso geralmente acontece quando um deploy anterior falhou")
                if _retried:
                    print("\n✗ Deploy falhou novamente após deletar a stack. Corrija manualmente.")
                    return False
                if _ask_yes_no("\n🗑️  Deseja deletar a stack e tentar novamente?", True):
                    if self.delete():
                        print("\n✓ Stack deletada. Executando deploy novamente...\n")
                        return self.deploy(guided, _retried=True)
                    print("\n✗ Erro ao deletar stack")
                else:
                    print("\n💡 Execute 'sam delete' manualmente ou corrija o estado no console AWS")
                return False

            print("\n✗ Erro no deploy")
            return False
        except FileNotFoundError:
            print("\n✗ SAM CLI não encontrado!")
            return False

    def delete(self) -> bool:
        """Run ``sam delete --no-prompts``.  Returns ``True`` on success."""
        print("\n🗑️  Deletando stack...")
        try:
            result = subprocess.run(
                ["sam", "delete", "--no-prompts"],
                cwd=self.project_root,
                env=self._aws.get_env(),
                capture_output=True,
                text=True,
                check=False,
            )
            if result.stdout:
                print(result.stdout)
            if result.stderr:
                print(result.stderr)
            return result.returncode == 0
        except Exception as exc:
            print(f"✗ Erro ao deletar: {exc}")
            return False


# ---------------------------------------------------------------------------
# Route 53 DNS management
# ---------------------------------------------------------------------------

class Route53Manager:
    """Route 53 DNS operations (zone listing, alias records, validation records).

    Parameters
    ----------
    aws:
        Shared :class:`AWSClientManager` instance.
    """

    def __init__(self, aws: AWSClientManager) -> None:
        self._aws = aws

    # ── helpers ────────────────────────────────────────────────────────────

    @staticmethod
    def _normalize(name: str) -> str:
        return name.strip().rstrip(".").lower()

    # ── public API ─────────────────────────────────────────────────────────

    def list_public_zones(self) -> List[Dict[str, Any]]:
        """Return all public hosted zones, sorted alphabetically by name."""
        zones: List[Dict[str, Any]] = []
        try:
            paginator = self._aws.route53.get_paginator("list_hosted_zones")
            for page in paginator.paginate():
                for zone in page.get("HostedZones", []):
                    if zone.get("Config", {}).get("PrivateZone"):
                        continue
                    zones.append({
                        "name": self._normalize(zone["Name"]),
                        "id": zone["Id"].replace("/hostedzone/", ""),
                        "record_count": zone.get("ResourceRecordSetCount", 0),
                    })
        except ClientError as exc:
            print(f"⚠ Erro ao listar domínios no Route 53: {exc}")
        zones.sort(key=lambda z: z["name"])
        return zones

    def find_zone_for_domain(self, domain: str) -> Optional[Dict[str, Any]]:
        """Return the most-specific public hosted zone that contains *domain*."""
        normalized = self._normalize(domain)
        matches = [
            z for z in self.list_public_zones()
            if normalized == z["name"] or normalized.endswith(f".{z['name']}")
        ]
        return max(matches, key=lambda z: len(z["name"])) if matches else None

    def upsert_alias_records(
        self, domain: str, hosted_zone_id: str, cloudfront_domain: str
    ) -> bool:
        """Create/update A and AAAA alias records pointing *domain* to CloudFront.

        Returns ``True`` on success.
        """
        changes = [
            {
                "Action": "UPSERT",
                "ResourceRecordSet": {
                    "Name": domain,
                    "Type": rtype,
                    "AliasTarget": {
                        "HostedZoneId": CLOUDFRONT_HOSTED_ZONE_ID,
                        "DNSName": cloudfront_domain,
                        "EvaluateTargetHealth": False,
                    },
                },
            }
            for rtype in ("A", "AAAA")
        ]
        try:
            resp = self._aws.route53.change_resource_record_sets(
                HostedZoneId=hosted_zone_id,
                ChangeBatch={"Comment": f"Apontar {domain} para CloudFront", "Changes": changes},
            )
            print("✓ DNS A/AAAA configurado para o CloudFront")
            self._wait_for_change(resp["ChangeInfo"]["Id"], f"{domain} -> CloudFront")
            return True
        except ClientError as exc:
            print(f"✗ Erro ao configurar DNS no Route 53: {exc}")
            return False

    def upsert_validation_record(
        self,
        hosted_zone_id: str,
        domain: str,
        record: Dict[str, str],
    ) -> bool:
        """Create/update the DNS record required for ACM certificate validation.

        Returns ``True`` on success.
        """
        if not hosted_zone_id:
            print("⚠ Sem hosted zone disponível para criar validação automaticamente.")
            return False
        print("\n🌎 Criando/atualizando validação DNS no Route 53...")
        try:
            resp = self._aws.route53.change_resource_record_sets(
                HostedZoneId=hosted_zone_id,
                ChangeBatch={
                    "Comment": f"Validação ACM para {domain}",
                    "Changes": [{
                        "Action": "UPSERT",
                        "ResourceRecordSet": {
                            "Name": record["Name"],
                            "Type": record["Type"],
                            "TTL": 300,
                            "ResourceRecords": [{"Value": record["Value"]}],
                        },
                    }],
                },
            )
            print("✓ Registro de validação criado/atualizado")
            self._wait_for_change(resp["ChangeInfo"]["Id"], "validação ACM")
            return True
        except ClientError as exc:
            print(f"✗ Erro ao criar validação DNS: {exc}")
            return False

    def _wait_for_change(
        self,
        change_id: str,
        description: str,
        max_wait: int = 300,
        interval: int = 10,
    ) -> bool:
        """Block until a Route 53 change reaches ``INSYNC``."""
        if not change_id:
            return False

        def check() -> Optional[bool]:
            try:
                status = self._aws.route53.get_change(Id=change_id)["ChangeInfo"]["Status"]
                if status == "INSYNC":
                    print(f"✓ Alteração Route 53 sincronizada ({description})")
                    return True
                print(f"  Aguardando Route 53 ({description})... Status: {status}")
            except ClientError as exc:
                print(f"⚠ Erro ao consultar alteração do Route 53: {exc}")
            return None

        return bool(_poll_until(check, f"Route 53 {description}", max_wait, interval, silent_first=False))


# ---------------------------------------------------------------------------
# ACM certificate management
# ---------------------------------------------------------------------------

class ACMManager:
    """Manages ACM certificates in ``us-east-1`` for use with CloudFront.

    Parameters
    ----------
    aws:
        Shared :class:`AWSClientManager` instance.
    route53:
        :class:`Route53Manager` used to create DNS validation records.
    """

    _VALIDATION_POLL_INTERVAL = 5
    _VALIDATION_MAX_RETRIES = 12
    _ISSUANCE_MAX_WAIT = 600
    _ISSUANCE_INTERVAL = 15

    def __init__(self, aws: AWSClientManager, route53: Route53Manager) -> None:
        self._aws = aws
        self._route53 = route53

    # ── helpers ────────────────────────────────────────────────────────────

    @staticmethod
    def _normalize(name: str) -> str:
        return name.strip().rstrip(".").lower()

    @staticmethod
    def _cert_covers(cert_name: str, domain: str) -> bool:
        """Return ``True`` if *cert_name* covers *domain* (exact or wildcard match)."""
        c = ACMManager._normalize(cert_name)
        d = ACMManager._normalize(domain)
        if c == d:
            return True
        if c.startswith("*."):
            suffix = c[2:]
            return d.endswith(f".{suffix}") and d.count(".") == suffix.count(".") + 1
        return False

    def _find_certificate(self, domain: str, status: str) -> Optional[str]:
        """Return the ARN of the first certificate with *status* that covers *domain*."""
        try:
            paginator = self._aws.acm_us_east_1.get_paginator("list_certificates")
            for page in paginator.paginate(CertificateStatuses=[status]):
                for cert in page.get("CertificateSummaryList", []):
                    names = [cert.get("DomainName", "")]
                    names.extend(cert.get("SubjectAlternativeNameSummaries", []))
                    if any(self._cert_covers(n, domain) for n in names if n):
                        return cert.get("CertificateArn")
        except ClientError as exc:
            print(f"⚠ Erro ao listar certificados ACM ({status}): {exc}")
        return None

    def find_issued(self, domain: str) -> Optional[str]:
        """Return the ARN of an already-issued certificate covering *domain*."""
        return self._find_certificate(domain, "ISSUED")

    def find_pending(self, domain: str) -> Optional[str]:
        """Return the ARN of a certificate pending DNS validation for *domain*."""
        return self._find_certificate(domain, "PENDING_VALIDATION")

    # ── public API ─────────────────────────────────────────────────────────

    def ensure_certificate(self, domain: str, hosted_zone_id: str) -> Optional[str]:
        """Return a validated ACM certificate ARN for *domain*.

        Steps when no issued certificate exists:
        1. Reuse a pending certificate, or request a new one.
        2. Create the DNS validation CNAME record in Route 53.
        3. Poll until the certificate is issued (up to 10 minutes).

        Returns ``None`` on any unrecoverable error.
        """
        arn = self.find_issued(domain)
        if arn:
            print(f"✓ Certificado ACM emitido encontrado: {arn}")
            return arn

        arn = self.find_pending(domain)
        if arn:
            print(f"⚠ Certificado ACM pendente encontrado: {arn}")
        else:
            print(f"\n🔐 Solicitando certificado ACM em us-east-1 para {domain}...")
            try:
                arn = self._aws.acm_us_east_1.request_certificate(
                    DomainName=domain, ValidationMethod="DNS"
                )["CertificateArn"]
                print(f"✓ Certificado solicitado: {arn}")
            except ClientError as exc:
                print(f"✗ Erro ao solicitar certificado ACM: {exc}")
                return None

        validation_record = self._wait_for_validation_record(arn)
        if validation_record is None:
            return None  # unrecoverable error
        if not validation_record:
            return arn  # {} sentinel: cert already ISSUED, nothing more to do

        print("\n🧾 Registro DNS de validação:")
        print(f"  Tipo:  {validation_record['Type']}")
        print(f"  Nome:  {validation_record['Name']}")
        print(f"  Valor: {validation_record['Value']}")

        if not self._route53.upsert_validation_record(hosted_zone_id, domain, validation_record):
            return None

        return self._wait_for_issuance(arn)

    def _wait_for_validation_record(self, arn: str) -> Optional[Dict[str, str]]:
        """Poll ACM until the DNS validation record data becomes available.

        Returns
        -------
        dict
            Non-empty: the ``{Type, Name, Value}`` CNAME record to upsert.
            Empty (``{}``): certificate is already ISSUED — no DNS action needed.
        None
            Unrecoverable error; the caller should abort.
        """
        print("⏳ Aguardando dados de validação DNS do ACM...")
        for _ in range(self._VALIDATION_MAX_RETRIES):
            try:
                cert = self._aws.acm_us_east_1.describe_certificate(
                    CertificateArn=arn
                )["Certificate"]
                if cert.get("Status") == "ISSUED":
                    print("✓ Certificado já está emitido")
                    return {}  # sentinel: already issued, skip DNS validation step
                for option in cert.get("DomainValidationOptions", []):
                    record = option.get("ResourceRecord")
                    if record:
                        return record
            except ClientError as exc:
                print(f"⚠ Erro ao consultar certificado ACM: {exc}")
                return None
            time.sleep(self._VALIDATION_POLL_INTERVAL)
        print("✗ Não foi possível obter o registro DNS de validação do ACM.")
        return None

    def _wait_for_issuance(self, arn: str) -> Optional[str]:
        """Poll until the certificate status becomes ``ISSUED``."""
        print("\n⏳ Aguardando certificado ACM ser emitido...")

        def check() -> Optional[str]:
            try:
                status = self._aws.acm_us_east_1.describe_certificate(
                    CertificateArn=arn
                )["Certificate"]["Status"]
                return arn if status == "ISSUED" else None
            except ClientError:
                return None

        result = _poll_until(
            check, "emissão do certificado ACM",
            self._ISSUANCE_MAX_WAIT, self._ISSUANCE_INTERVAL,
        )
        if result:
            print("✓ Certificado emitido com sucesso!")
            return arn
        print(f"  ARN: {arn}")
        return None


# ---------------------------------------------------------------------------
# CloudFront distribution management
# ---------------------------------------------------------------------------

class CloudFrontManager:
    """Creates and updates CloudFront distributions for Lambda Function URLs.

    Parameters
    ----------
    aws:
        Shared :class:`AWSClientManager` instance.
    """

    _DEPLOY_MAX_WAIT = 1800
    _DEPLOY_INTERVAL = 30

    def __init__(self, aws: AWSClientManager) -> None:
        self._aws = aws

    # ── static helpers ─────────────────────────────────────────────────────

    @staticmethod
    def _normalize(name: str) -> str:
        return name.strip().rstrip(".").lower()

    @staticmethod
    def _origin_domain(function_url: str) -> str:
        """Extract the hostname from a Lambda Function URL."""
        parsed = urlparse(function_url)
        return parsed.netloc or function_url.replace("https://", "").replace("http://", "").strip("/")

    @staticmethod
    def _normalize_path(value: str) -> str:
        path = value.strip()
        return "/" if not path or path == "/" else "/" + path.strip("/")

    @staticmethod
    def _path_patterns(path_prefix: str) -> List[str]:
        """Return the CloudFront path patterns that cover *path_prefix*."""
        norm = CloudFrontManager._normalize_path(path_prefix)
        return [] if norm == "/" else [norm, f"{norm}/*"]

    @staticmethod
    def _build_origin(origin_id: str, function_url: str) -> Dict[str, Any]:
        """Construct a CloudFront origin dict pointing to a Lambda Function URL."""
        domain = CloudFrontManager._origin_domain(function_url)
        return {
            "Id": origin_id,
            "DomainName": domain,
            "OriginPath": "",
            "CustomHeaders": {"Quantity": 0},
            "CustomOriginConfig": {
                "HTTPPort": 80,
                "HTTPSPort": 443,
                "OriginProtocolPolicy": "https-only",
                "OriginSslProtocols": {"Quantity": 1, "Items": ["TLSv1.2"]},
                "OriginReadTimeout": 30,
                "OriginKeepaliveTimeout": 5,
            },
            "ConnectionAttempts": 3,
            "ConnectionTimeout": 10,
            "OriginShield": {"Enabled": False},
        }

    @staticmethod
    def _build_cache_behavior(
        origin_id: str, path_pattern: Optional[str] = None
    ) -> Dict[str, Any]:
        """Build a cache behavior that passes all requests through to the origin."""
        behavior: Dict[str, Any] = {
            "TargetOriginId": origin_id,
            "TrustedSigners": {"Enabled": False, "Quantity": 0},
            "TrustedKeyGroups": {"Enabled": False, "Quantity": 0},
            "ViewerProtocolPolicy": "redirect-to-https",
            "AllowedMethods": {
                "Quantity": 7,
                "Items": ["GET", "HEAD", "OPTIONS", "PUT", "POST", "PATCH", "DELETE"],
                "CachedMethods": {"Quantity": 2, "Items": ["GET", "HEAD"]},
            },
            "SmoothStreaming": False,
            "Compress": True,
            "LambdaFunctionAssociations": {"Quantity": 0},
            "FunctionAssociations": {"Quantity": 0},
            "FieldLevelEncryptionId": "",
            "CachePolicyId": CLOUDFRONT_CACHE_POLICY_DISABLED,
            "OriginRequestPolicyId": CLOUDFRONT_ORIGIN_REQUEST_ALL_VIEWER_EXCEPT_HOST,
        }
        if path_pattern:
            behavior["PathPattern"] = path_pattern
        return behavior

    @staticmethod
    def _behavior_captures(existing_pattern: str, desired_pattern: str) -> bool:
        """Return ``True`` if *existing_pattern* would also match *desired_pattern*.

        Used to detect ordering conflicts when inserting a new behavior.
        """
        if not existing_pattern:
            return False
        ep = existing_pattern if existing_pattern.startswith("/") else f"/{existing_pattern}"
        dp = desired_pattern if desired_pattern.startswith("/") else f"/{desired_pattern}"
        if ep in ("*", "/*"):
            return True
        if "*" not in ep:
            return False
        ep_prefix = ep.split("*", 1)[0].rstrip("/")
        dp_prefix = dp.split("*", 1)[0].rstrip("/")
        return dp_prefix == ep_prefix or dp_prefix.startswith(f"{ep_prefix}/")

    @staticmethod
    def _slug(name: str) -> str:
        """Convert *name* to a URL-safe alphanumeric slug."""
        return re.sub(r"[^a-z0-9]+", "", name.lower().replace("_", "").replace("-", ""))

    # ── public API ─────────────────────────────────────────────────────────

    def find_distribution_by_alias(self, domain: str) -> Optional[Dict[str, Any]]:
        """Return the distribution that already uses *domain* as an alias, or ``None``."""
        normalized = self._normalize(domain)
        try:
            paginator = self._aws.cloudfront.get_paginator("list_distributions")
            for page in paginator.paginate():
                for item in page.get("DistributionList", {}).get("Items", []):
                    aliases = [
                        self._normalize(a)
                        for a in item.get("Aliases", {}).get("Items", [])
                    ]
                    if normalized in aliases:
                        return item
        except ClientError as exc:
            print(f"⚠ Erro ao listar distribuições CloudFront: {exc}")
        return None

    def get_distribution_config(
        self, distribution_id: str
    ) -> Tuple[Optional[Dict[str, Any]], Optional[str]]:
        """Fetch a distribution config.  Returns ``(config, etag)`` or ``(None, None)``."""
        try:
            resp = self._aws.cloudfront.get_distribution_config(Id=distribution_id)
            return resp["DistributionConfig"], resp["ETag"]
        except ClientError as exc:
            print(f"✗ Erro ao baixar configuração do CloudFront: {exc}")
            return None, None

    def get_domain(self, distribution_id: str) -> Optional[str]:
        """Return the ``*.cloudfront.net`` domain name for *distribution_id*."""
        try:
            return self._aws.cloudfront.get_distribution(
                Id=distribution_id
            )["Distribution"]["DomainName"]
        except ClientError:
            return None

    def show_summary(self, distribution_id: str, config: Dict[str, Any]) -> None:
        """Print a human-readable summary of an existing distribution."""
        print("\n📥 Configuração atual do CloudFront baixada")
        print("-" * 60)
        print(f"  Distribuição: {distribution_id}")
        aliases = config.get("Aliases", {}).get("Items", [])
        print(f"  Aliases: {', '.join(aliases) if aliases else '(nenhum)'}")
        print("\n  Origins:")
        for o in config.get("Origins", {}).get("Items", []):
            print(f"    - {o.get('Id')} -> {o.get('DomainName')}")
        default_target = config.get("DefaultCacheBehavior", {}).get("TargetOriginId", "(não informado)")
        print(f"\n  Default behavior -> {default_target}")
        behaviors = config.get("CacheBehaviors", {}).get("Items", [])
        if behaviors:
            print("\n  Cache behaviors existentes:")
            for b in behaviors[:30]:
                print(f"    - {b.get('PathPattern')} -> {b.get('TargetOriginId')}")
            if len(behaviors) > 30:
                print(f"    ... e mais {len(behaviors) - 30} behavior(s)")
        else:
            print("\n  Cache behaviors existentes: nenhum")
        print("-" * 60)

    def backup_config(
        self,
        domain: str,
        distribution_id: str,
        config: Dict[str, Any],
        backup_dir: Path,
        *,
        etag: Optional[str] = None,
        cloudfront_domain: Optional[str] = None,
        reason: str = "before-update",
    ) -> Optional[Path]:
        """Save a restorable JSON backup of *config* to *backup_dir*.

        The backup includes the ETag and a restore note so the config can be
        reapplied manually via ``cloudfront.update_distribution``.
        """
        backup_dir.mkdir(exist_ok=True)
        now = datetime.now(timezone.utc)
        ts = now.strftime("%Y%m%dT%H%M%SZ")
        safe_domain = re.sub(r"[^A-Za-z0-9.-]+", "-", self._normalize(domain)).strip(".-")
        stem = f"{safe_domain or 'cloudfront'}_{ts}"
        backup_file = backup_dir / f"{stem}.json"
        counter = 2
        while backup_file.exists():
            backup_file = backup_dir / f"{stem}-{counter}.json"
            counter += 1

        payload = {
            "schema_version": 1,
            "saved_at_iso": now.replace(microsecond=0).isoformat().replace("+00:00", "Z"),
            "reason": reason,
            "domain": self._normalize(domain),
            "distribution_id": distribution_id,
            "cloudfront_domain": cloudfront_domain,
            "etag_at_backup": etag,
            "restore_notes": (
                "Para restaurar, use distribution_config deste arquivo com "
                "cloudfront.update_distribution e o ETag atual da distribuição."
            ),
            "distribution_config": config,
        }
        try:
            backup_file.write_text(
                json.dumps(payload, indent=2, ensure_ascii=False), encoding="utf-8"
            )
            print(f"✓ Backup CloudFront salvo automaticamente em {backup_file}")
            return backup_file
        except OSError as exc:
            print(f"⚠ Não foi possível salvar backup local do CloudFront: {exc}")
            return None

    def ensure_origin(
        self,
        config: Dict[str, Any],
        function_url: str,
        function_name: str,
    ) -> Tuple[str, bool]:
        """Guarantee that a Function URL origin exists in the distribution config.

        Returns ``(origin_id, was_added)`` where *was_added* is ``True`` when
        a new origin entry was inserted.
        """
        origin_domain = self._origin_domain(function_url)
        origins = config.setdefault("Origins", {"Quantity": 0})
        items: List[Dict[str, Any]] = origins.setdefault("Items", [])

        for origin in items:
            if self._normalize(origin.get("DomainName", "")) == self._normalize(origin_domain):
                print(f"✓ Origin existente reutilizado: {origin['Id']} -> {origin_domain}")
                return origin["Id"], False

        slug = self._slug(function_name) or "Function"
        origin_id = f"LambdaUrl-{slug}"
        existing_ids = {o.get("Id") for o in items}
        suffix = 2
        while origin_id in existing_ids:
            origin_id = f"LambdaUrl-{slug}-{suffix}"
            suffix += 1

        items.append(self._build_origin(origin_id, function_url))
        origins["Quantity"] = len(items)
        print(f"✓ Novo origin adicionado: {origin_id} -> {origin_domain}")
        return origin_id, True

    def upsert_behaviors(
        self,
        config: Dict[str, Any],
        origin_id: str,
        path_prefix: str,
        is_new_distribution: bool = False,
    ) -> Optional[bool]:
        """Add or update cache behaviors for *path_prefix* in *config*.

        Returns
        -------
        ``True``   — changes were made.
        ``False``  — no changes needed.
        ``None``   — user cancelled due to a conflict.
        """
        normalized = self._normalize_path(path_prefix)

        if normalized == "/":
            current_origin = config.get("DefaultCacheBehavior", {}).get("TargetOriginId")
            if not is_new_distribution:
                print(f"\n⚠ O caminho '/' altera o default behavior atual ({current_origin}).")
                if not _ask_yes_no("   Deseja substituir o default behavior por esta Lambda?", False):
                    print("Operação cancelada para evitar sobrescrever roteamento existente.")
                    return None
            config["DefaultCacheBehavior"] = self._build_cache_behavior(origin_id)
            return True

        cache_behaviors = config.setdefault("CacheBehaviors", {"Quantity": 0})
        items: List[Dict[str, Any]] = cache_behaviors.setdefault("Items", [])
        changed = False
        confirmed_broad: set = set()

        for pattern in self._path_patterns(normalized):
            existing_idx = next(
                (i for i, b in enumerate(items) if b.get("PathPattern") == pattern), None
            )
            new_behavior = self._build_cache_behavior(origin_id, pattern)

            if existing_idx is not None:
                existing_origin = items[existing_idx].get("TargetOriginId")
                if existing_origin == origin_id:
                    print(f"✓ Behavior já aponta para esta Lambda: {pattern}")
                    continue
                print(f"\n⚠ Behavior existente encontrado: {pattern} -> {existing_origin}")
                print("   Atualizar este behavior pode mudar tráfego que já existe.")
                if not _ask_yes_no("   Deseja atualizar mesmo assim?", False):
                    print("Operação cancelada para preservar behavior existente.")
                    return None
                items[existing_idx] = new_behavior
                changed = True
                continue

            insert_at = len(items)
            for i, b in enumerate(items):
                ep = b.get("PathPattern", "")
                if self._behavior_captures(ep, pattern):
                    if ep not in confirmed_broad:
                        print(f"\n⚠ O behavior amplo '{ep}' também pode atender '{pattern}'.")
                        print("   Para o novo link funcionar, ele precisa ficar antes desse behavior.")
                        if not _ask_yes_no("   Deseja inserir o novo behavior antes dele?", True):
                            print("Operação cancelada para não alterar a ordem dos behaviors.")
                            return None
                        confirmed_broad.add(ep)
                    insert_at = i
                    break

            items.insert(insert_at, new_behavior)
            changed = True
            print(f"✓ Behavior adicionado: {pattern} -> {origin_id}")

        cache_behaviors["Quantity"] = len(items)
        return changed

    def build_new_config(
        self,
        domain: str,
        function_url: str,
        certificate_arn: str,
        function_name: str,
        path_prefix: str,
    ) -> Dict[str, Any]:
        """Return a complete CloudFront distribution configuration for a new distribution."""
        slug = self._slug(function_name) or "Function"
        origin_id = f"LambdaUrl-{slug}"
        path_behaviors = [
            self._build_cache_behavior(origin_id, p)
            for p in self._path_patterns(path_prefix)
        ]
        cache_behaviors: Dict[str, Any] = {"Quantity": len(path_behaviors)}
        if path_behaviors:
            cache_behaviors["Items"] = path_behaviors

        return {
            "CallerReference": f"lambda-deployer-{int(time.time())}",
            "Aliases": {"Quantity": 1, "Items": [domain]},
            "DefaultRootObject": "",
            "Origins": {
                "Quantity": 1,
                "Items": [self._build_origin(origin_id, function_url)],
            },
            "OriginGroups": {"Quantity": 0},
            "DefaultCacheBehavior": self._build_cache_behavior(origin_id),
            "CacheBehaviors": cache_behaviors,
            "CustomErrorResponses": {"Quantity": 0},
            "Comment": f"Lambda Function URL - {domain}",
            "Logging": {"Enabled": False, "IncludeCookies": False, "Bucket": "", "Prefix": ""},
            "PriceClass": "PriceClass_100",
            "Enabled": True,
            "ViewerCertificate": {
                "ACMCertificateArn": certificate_arn,
                "SSLSupportMethod": "sni-only",
                "MinimumProtocolVersion": "TLSv1.2_2021",
                "Certificate": certificate_arn,
                "CertificateSource": "acm",
            },
            "Restrictions": {"GeoRestriction": {"RestrictionType": "none", "Quantity": 0}},
            "WebACLId": "",
            "HttpVersion": "http2and3",
            "IsIPV6Enabled": True,
        }

    def create_distribution(self, config: Dict[str, Any]) -> Optional[Dict[str, Any]]:
        """Call the CloudFront API to create a new distribution.

        Returns the full API response dict on success, or ``None`` on error.
        """
        try:
            return self._aws.cloudfront.create_distribution(DistributionConfig=config)
        except ClientError as exc:
            print(f"✗ Erro ao criar CloudFront: {exc}")
            return None

    def update_distribution(
        self, distribution_id: str, etag: str, config: Dict[str, Any]
    ) -> Optional[str]:
        """Update an existing distribution.

        Returns the CloudFront domain name on success, or ``None`` on error.
        """
        try:
            resp = self._aws.cloudfront.update_distribution(
                Id=distribution_id, IfMatch=etag, DistributionConfig=config
            )
            return resp["Distribution"]["DomainName"]
        except ClientError as exc:
            print(f"✗ Erro ao atualizar CloudFront: {exc}")
            return None

    def wait_until_deployed(self, distribution_id: str) -> bool:
        """Block until the distribution status reaches ``Deployed``."""
        print("\n⏳ Aguardando CloudFront ficar Deployed...")

        def check() -> Optional[bool]:
            try:
                status = self._aws.cloudfront.get_distribution(
                    Id=distribution_id
                )["Distribution"]["Status"]
                if status == "Deployed":
                    print("✓ CloudFront está Deployed")
                    return True
                print(f"  Aguardando CloudFront... Status: {status}")
            except ClientError as exc:
                print(f"✗ Erro ao consultar CloudFront: {exc}")
                return True  # stop on error
            return None

        return bool(
            _poll_until(
                check, "CloudFront Deployed",
                self._DEPLOY_MAX_WAIT, self._DEPLOY_INTERVAL, silent_first=False,
            )
        )

    def verify_link(self, domain: str, path_prefix: str, distribution_id: str) -> None:
        """Run post-deploy sanity checks on the CloudFront link and print results."""
        print("\n" + "=" * 60)
        print("🔎 VERIFICAÇÃO DO LINK CLOUDFRONT")
        print("=" * 60)

        counts: Dict[str, int] = {"ok": 0, "warn": 0, "fail": 0}

        def ok(msg: str) -> None:
            counts["ok"] += 1
            print(f"  ✓ {msg}")

        def warn(msg: str) -> None:
            counts["warn"] += 1
            print(f"  ⚠ {msg}")

        def fail(msg: str) -> None:
            counts["fail"] += 1
            print(f"  ✗ {msg}")

        try:
            dist = self._aws.cloudfront.get_distribution(Id=distribution_id)["Distribution"]
            cfg = dist["DistributionConfig"]
            aliases = [self._normalize(a) for a in cfg.get("Aliases", {}).get("Items", [])]
            if dist.get("Status") == "Deployed" and cfg.get("Enabled"):
                ok(f"CloudFront ativo: {distribution_id}")
            else:
                warn(f"CloudFront status={dist.get('Status')} enabled={cfg.get('Enabled')}")
            if self._normalize(domain) in aliases:
                ok(f"Alias configurado: {domain}")
            else:
                fail(f"Alias {domain} não está na distribuição")
        except ClientError as exc:
            fail(f"Erro consultando CloudFront: {exc}")

        try:
            ip = socket.gethostbyname(domain)
            ok(f"DNS resolvendo: {domain} -> {ip}")
        except OSError:
            warn(f"DNS ainda não resolveu {domain}; pode ser propagação")

        norm = self._normalize_path(path_prefix)
        check_url = f"https://{domain}{norm}"
        try:
            req = urllib.request.Request(
                check_url, headers={"User-Agent": "lambda-deployer-check/1.0"}, method="GET"
            )
            with urllib.request.urlopen(req, timeout=15) as response:
                ok(f"URL respondeu HTTP {response.status}: {check_url}")
        except urllib.error.HTTPError as exc:
            if 400 <= exc.code < 500:
                warn(f"URL respondeu HTTP {exc.code}; CloudFront alcançável mas origem retornou erro")
            else:
                warn(f"URL respondeu HTTP {exc.code}: {check_url}")
        except Exception as exc:
            warn(f"Não foi possível acessar {check_url}: {exc}")

        print("-" * 60)
        print(f"Resultado: {counts['ok']} ok, {counts['warn']} aviso(s), {counts['fail']} falha(s)")
        print("=" * 60)


# ---------------------------------------------------------------------------
# Main orchestrator
# ---------------------------------------------------------------------------

class LambdaDeployer:
    """Interactive orchestrator for the full Lambda deploy workflow.

    Responsibilities
    ----------------
    * Gather configuration from the user (or reuse a saved config).
    * Generate ``template.yaml`` and ``samconfig.toml``.
    * Delegate build/deploy to :class:`SAMRunner`.
    * Delegate CloudFront / DNS wiring to the appropriate managers.
    * Persist configuration across runs via :class:`ConfigStore`.

    All AWS API calls are routed through the specialised manager classes;
    this class focuses on orchestration and user interaction only.
    """

    def __init__(self, target_dir: Optional[Path] = None) -> None:
        self.invocation_root = target_dir.resolve() if target_dir else Path.cwd()
        self.project_root = self.invocation_root
        # When target_dir is given explicitly, the SAM root is fixed — never inferred.
        self._explicit_target = target_dir is not None
        self.template_file = self.project_root / "template.yaml"
        self.samconfig_file = self.project_root / "samconfig.toml"

        # Resolved during configure_aws_profile()
        self.aws_profile: Optional[str] = None
        self.aws_region: Optional[str] = None

        # Managers — instantiated after profile selection
        self._aws: Optional[AWSClientManager] = None
        self._iam: Optional[IAMManager] = None
        self._sam: Optional[SAMRunner] = None
        self._route53: Optional[Route53Manager] = None
        self._acm: Optional[ACMManager] = None
        self._cf: Optional[CloudFrontManager] = None
        self._config_store = ConfigStore(self.project_root / ".lambda_deployer_config.json")

        # Set to True when the saved config is used as-is (skips interactive prompts).
        self._using_saved_config = False

    def _set_sam_root(self, sam_root: Path) -> None:
        """Set the directory that contains all SAM files and Lambda root files."""
        self.project_root = sam_root.resolve()
        self.template_file = self.project_root / "template.yaml"
        self.samconfig_file = self.project_root / "samconfig.toml"
        self._config_store = ConfigStore(self.project_root / ".lambda_deployer_config.json")
        if self._aws:
            self._sam = SAMRunner(self.project_root, self._aws)

    def _resolve_sam_root(self, code_file: str) -> Path:
        """Return the directory that should be treated as the Lambda/SAM root."""
        path = Path(code_file)
        if not path.is_absolute():
            path = self.invocation_root / path

        wrapper = self.invocation_root / "handler.py"
        if wrapper.exists() and path.resolve() != wrapper.resolve():
            module = self._module_name_from_path(path, self.invocation_root)
            if self._root_wrapper_exports(wrapper, module):
                return self.invocation_root

        return path.parent

    def _normalise_code_file_for_sam_root(self, code_file: str) -> str:
        """Return *code_file* as a path relative to the current SAM root."""
        path = Path(code_file)
        if not path.is_absolute():
            path = self.invocation_root / path

        try:
            return str(path.resolve().relative_to(self.project_root.resolve()))
        except ValueError:
            return str(path.resolve())

    def _apply_sam_root_from_config(self, config: Dict[str, Any]) -> None:
        """Make the configured SAM root active for file generation and SAM CLI."""
        if self._explicit_target:
            config["sam_root"] = str(self.project_root)
            return

        raw_root = config.get("sam_root")
        if raw_root:
            sam_root = Path(raw_root)
            if not sam_root.is_absolute():
                sam_root = self.invocation_root / sam_root
        else:
            sam_root = self._resolve_sam_root(config["code_file"])

        self._set_sam_root(sam_root)
        config["sam_root"] = str(self.project_root)

    @staticmethod
    def _module_name_from_path(path: Path, root: Path) -> str:
        """Return the Python module path for *path* relative to *root*."""
        try:
            relative = path.resolve().relative_to(root.resolve())
        except ValueError:
            return path.stem

        without_suffix = relative.with_suffix("")
        parts = list(without_suffix.parts)
        if parts and parts[-1] == "__init__":
            parts = parts[:-1]
        return ".".join(parts) or path.stem

    @staticmethod
    def _root_wrapper_exports(wrapper: Path, module: str) -> bool:
        """Return True when *wrapper* reexports ``handler`` from *module*."""
        try:
            tree = ast.parse(wrapper.read_text(encoding="utf-8"))
        except Exception:
            return False

        for node in ast.walk(tree):
            if not isinstance(node, ast.ImportFrom) or node.module != module:
                continue
            for alias in node.names:
                exported_name = alias.asname or alias.name
                if alias.name == "handler" and exported_name == "handler":
                    return True
        return False

    # ── setup ──────────────────────────────────────────────────────────────

    def configure_aws_profile(self) -> None:
        """Select an AWS CLI profile.

        Uses the saved profile silently when available.  Only prompts
        interactively when there is no saved profile or it no longer exists.
        """
        saved_config = self._config_store.load()
        saved_profile = saved_config.get("aws_profile") if saved_config else None

        selector = ProfileSelector()

        if saved_profile:
            profiles = ProfileSelector.load_profiles()
            if saved_profile in profiles:
                region = ProfileSelector.get_profile_region(saved_profile)
                os.environ["AWS_PROFILE"] = saved_profile
                os.environ.setdefault("AWS_SDK_LOAD_CONFIG", "1")
                if region:
                    os.environ.setdefault("AWS_DEFAULT_REGION", region)
                print(f"✓ Perfil AWS: {saved_profile}" + (f" [{region}]" if region else ""))
                self.aws_profile = saved_profile
                self.aws_region = region
                return

        self.aws_profile, self.aws_region = selector.select(saved_profile)

    def initialise_managers(self) -> None:
        """Create all AWS manager instances after the profile has been selected."""
        self._aws = AWSClientManager(profile=self.aws_profile, region=self.aws_region)

        # Touch the session so it resolves the region before managers use it.
        _ = self._aws.session
        self.aws_region = self._aws.region

        self._route53 = Route53Manager(self._aws)
        self._iam = IAMManager(self._aws)
        self._sam = SAMRunner(self.project_root, self._aws)
        self._acm = ACMManager(self._aws, self._route53)
        self._cf = CloudFrontManager(self._aws)

        try:
            # Warm up Lambda client to confirm credentials work.
            _ = self._aws.lambda_
            print("✓ Clientes AWS inicializados com sucesso")
        except Exception as exc:
            print(f"⚠ Aviso: Não foi possível inicializar clientes AWS: {exc}")
            print("  Continuando sem acesso direto à AWS...")

    # ── Lambda introspection ───────────────────────────────────────────────

    def _get_existing_lambda_config(self, function_name: str) -> Optional[Dict[str, Any]]:
        """Return the current configuration of an existing Lambda function, or ``None``."""
        if not self._aws:
            return None
        try:
            config = self._aws.lambda_.get_function(FunctionName=function_name)["Configuration"]
            return {
                "runtime": config.get("Runtime", DEFAULT_RUNTIME),
                "memory": config.get("MemorySize", DEFAULT_MEMORY_MB),
                "timeout": config.get("Timeout", DEFAULT_TIMEOUT_S),
                "handler": config.get("Handler", "app.lambda_handler"),
                "environment": config.get("Environment", {}).get("Variables", {}),
                "layers": config.get("Layers", []),
                "role": config.get("Role", ""),
                "description": config.get("Description", ""),
            }
        except ClientError as exc:
            if exc.response["Error"]["Code"] != "ResourceNotFoundException":
                print(f"⚠ Erro ao buscar Lambda: {exc}")
            return None

    def _get_function_url(self, function_name: str) -> Optional[str]:
        """Return the Function URL of *function_name* if one is configured."""
        if not self._aws:
            return None
        try:
            return self._aws.lambda_.get_function_url_config(
                FunctionName=function_name
            )["FunctionUrl"]
        except ClientError:
            return None

    def _wait_for_function_url(self, function_name: str, max_wait: int = 90) -> Optional[str]:
        """Poll until the Function URL becomes available after deploy."""
        return _poll_until(
            lambda: self._get_function_url(function_name),
            "Function URL",
            max_wait,
            interval=5,
        )

    # ── code analysis ──────────────────────────────────────────────────────

    @staticmethod
    def _detect_mangum_handler(file_path: str) -> Optional[str]:
        """Return the variable name assigned to ``Mangum(...)`` in *file_path*, or ``None``."""
        try:
            tree = ast.parse(Path(file_path).read_text(encoding="utf-8"))
            for node in ast.walk(tree):
                if isinstance(node, ast.Assign) and isinstance(node.value, ast.Call):
                    func = node.value.func
                    func_name = (
                        func.id if isinstance(func, ast.Name)
                        else func.attr if isinstance(func, ast.Attribute)
                        else None
                    )
                    if func_name == "Mangum" and node.targets:
                        target = node.targets[0]
                        if isinstance(target, ast.Name):
                            return target.id
        except Exception:
            pass
        return None

    @staticmethod
    def _extract_functions(file_path: str) -> List[Dict[str, Any]]:
        """Return a list of function definitions found in *file_path*."""
        try:
            tree = ast.parse(Path(file_path).read_text(encoding="utf-8"))
        except SyntaxError as exc:
            print(f"⚠ Erro de sintaxe no arquivo: {exc}")
            return []
        except Exception as exc:
            print(f"⚠ Erro ao analisar arquivo: {exc}")
            return []

        functions = []
        for node in ast.walk(tree):
            if isinstance(node, ast.FunctionDef):
                doc = (ast.get_docstring(node) or "Sem descrição").split("\n")[0].strip()
                if len(doc) > 80:
                    doc = doc[:77] + "..."
                functions.append({
                    "name": node.name,
                    "description": doc,
                    "params": [a.arg for a in node.args.args],
                    "line": node.lineno,
                })
        functions.sort(key=lambda f: f["line"])
        return functions

    @staticmethod
    def _is_named_call(node: ast.AST, name: str) -> bool:
        """Return True when *node* is a call to a top-level name or attribute *name*."""
        if not isinstance(node, ast.Call):
            return False
        func = node.func
        func_name = (
            func.id if isinstance(func, ast.Name)
            else func.attr if isinstance(func, ast.Attribute)
            else None
        )
        return func_name == name

    @staticmethod
    def _call_has_keyword(call: ast.Call, keyword: str) -> bool:
        """Return True when *call* contains a keyword argument named *keyword*."""
        return any(kw.arg == keyword for kw in call.keywords)

    @staticmethod
    def _offset_from_position(source: str, line: int, column: int) -> int:
        """Convert a 1-based line and 0-based column pair to a string offset."""
        lines = source.splitlines(keepends=True)
        return sum(len(part) for part in lines[:line - 1]) + column

    @staticmethod
    def _insert_keyword_in_call_source(
        source: str,
        call: ast.Call,
        keyword: str,
        value: str,
    ) -> Optional[str]:
        """Return *source* with ``keyword=value`` inserted into *call*."""
        end_line = getattr(call, "end_lineno", None)
        end_col = getattr(call, "end_col_offset", None)
        if end_line is None or end_col is None:
            return None

        insert_at = LambdaDeployer._offset_from_position(source, end_line, end_col) - 1
        if insert_at < 0 or insert_at >= len(source) or source[insert_at] != ")":
            return None

        prev_idx = insert_at - 1
        while prev_idx >= 0 and source[prev_idx].isspace():
            prev_idx -= 1
        prev_char = source[prev_idx] if prev_idx >= 0 else ""

        value_literal = json.dumps(value)
        call_text = ast.get_source_segment(source, call) or ""
        if "\n" in call_text:
            lines = source.splitlines(keepends=True)
            closing_line = lines[end_line - 1] if end_line - 1 < len(lines) else ""
            closing_indent = re.match(r"[ \t]*", closing_line).group(0)
            line_start = LambdaDeployer._offset_from_position(source, end_line, 0)
            closing_prefix = source[line_start:insert_at]
            if closing_prefix.strip():
                comma = "," if prev_char not in ("", "(", ",") else ""
                insertion = f'{comma}\n{closing_indent}    {keyword}={value_literal},'
                return source[:insert_at] + insertion + source[insert_at:]

            prefix = source[:line_start]
            if prev_char not in ("", "(", ","):
                prefix = source[:prev_idx + 1] + "," + source[prev_idx + 1:line_start]
            insertion = f'{closing_indent}    {keyword}={value_literal},\n'
            return prefix + insertion + source[line_start:]
        else:
            if prev_char == "(":
                prefix = ""
            elif prev_char == ",":
                prefix = " "
            else:
                prefix = ", "
            insertion = f"{prefix}{keyword}={value_literal}"

        return source[:insert_at] + insertion + source[insert_at:]

    def _select_function_from_file(self, file_path: str) -> Optional[str]:
        """List functions in *file_path* and let the user pick one."""
        functions = self._extract_functions(file_path)
        if not functions:
            print("⚠ Nenhuma função encontrada no arquivo!")
            return None

        print(f"\n📋 Funções encontradas em {Path(file_path).name}:")
        print("-" * 80)
        for i, func in enumerate(functions, 1):
            params = ", ".join(func["params"])
            print(f"  {i}. {func['name']}({params})")
            print(f"     {func['description']}")
            if i < len(functions):
                print()
        print("-" * 80)
        print("\n💡 Digite o número, o nome da função, ou Enter para usar a primeira.")

        while True:
            choice = input("\nEscolha a função: ").strip()

            if not choice:
                return functions[0]["name"]

            if choice.isdigit():
                idx = int(choice) - 1
                if 0 <= idx < len(functions):
                    print(f"✓ Selecionado: {functions[idx]['name']}")
                    return functions[idx]["name"]
                print(f"⚠ Número inválido! Escolha entre 1 e {len(functions)}")
                continue

            if any(f["name"] == choice for f in functions):
                print(f"✓ Selecionado: {choice}")
                return choice

            all_names = [f["name"] for f in functions]
            print(f"⚠ Função '{choice}' não encontrada no arquivo!")
            print(f"   Funções disponíveis: {', '.join(all_names)}")
            if _ask_yes_no(f"   Deseja usar '{choice}' mesmo assim?", False):
                return choice

    # ── config collection sub-methods ──────────────────────────────────────

    def _collect_function_name(self, saved_config: Optional[Dict[str, Any]]) -> str:
        """Ask for the Lambda function name.

        Only collects the name; the caller is responsible for fetching the
        existing Lambda config once to avoid duplicate API calls.
        """
        default_name = saved_config.get("function_name") if saved_config else None
        return _ask("\n📝 Nome da função Lambda", default_name)

    def _collect_code_file(self, saved_config: Optional[Dict[str, Any]]) -> Optional[str]:
        """Ask for the Lambda entry-point Python file (validates existence).

        Returns ``None`` when the user declines to retry after a bad path.
        The caller is responsible for deciding how to handle cancellation.
        """
        print("\n" + "-" * 60)
        print("📂 ARQUIVO DO CÓDIGO")
        print("-" * 60)

        default_file = saved_config.get("code_file") if saved_config else "app.py"

        while True:
            file_input = _ask("\nCaminho do arquivo principal (.py)", default_file)
            file_path = Path(file_input)
            candidates = []
            if not file_path.is_absolute():
                saved_root = (saved_config or {}).get("sam_root")
                if saved_root:
                    saved_root_path = Path(saved_root)
                    if not saved_root_path.is_absolute():
                        saved_root_path = self.invocation_root / saved_root_path
                    candidates.append(saved_root_path / file_path)
                candidates.append(self.invocation_root / file_path)
            candidates.append(file_path)

            resolved_file = next((candidate for candidate in candidates if candidate.exists()), None)
            if resolved_file is None:
                print(f"⚠ Arquivo não encontrado: {file_input}")
                py_files = list(self.project_root.glob("*.py"))
                if py_files:
                    print("\n💡 Arquivos Python encontrados no diretório:")
                    for pf in py_files[:10]:
                        print(f"   - {pf.name}")
                if not _ask_yes_no("Tentar novamente?", True):
                    return None
                continue

            file_path = resolved_file
            if file_path.suffix != ".py":
                print("⚠ O arquivo deve ser um arquivo Python (.py)")
                if not _ask_yes_no("Tentar novamente?", True):
                    return None
                continue

            print(f"✓ Arquivo encontrado: {file_path}")
            return str(file_path)

    def _collect_handler(
        self,
        code_file: str,
        existing_config: Optional[Dict[str, Any]],
    ) -> str:
        """Detect or ask for the Lambda handler (module.function)."""
        print("\n" + "-" * 60)
        print("⚙️  FUNÇÃO HANDLER")
        print("-" * 60)

        module = self._module_name_from_code_file(code_file)
        root_wrapper = self._detect_root_handler_wrapper(code_file, module)
        if root_wrapper:
            print(f"\n🔍 Wrapper raiz detectado: {root_wrapper}")
            print(f"\n✓ Handler configurado: {root_wrapper}")
            return root_wrapper

        # Auto-detect Mangum handler
        mangum_var = self._detect_mangum_handler(code_file)
        if mangum_var:
            mangum_handler = f"{module}.{mangum_var}"
            print(f"\n🔍 Handler Mangum detectado: {mangum_handler}")
            if _ask_yes_no(f"   Deseja usar '{mangum_handler}' como handler?", True):
                print(f"\n✓ Handler configurado: {mangum_handler}")
                return mangum_handler

        # Let user pick a function from the file
        selected = self._select_function_from_file(code_file)
        if not selected:
            default_func = (
                existing_config["handler"].split(".")[-1]
                if existing_config
                else "lambda_handler"
            )
            selected = _ask("\nNome da função handler", default_func)

        handler = f"{module}.{selected}"
        print(f"\n✓ Handler configurado: {handler}")
        return handler

    def _detect_root_handler_wrapper(self, code_file: str, module: str) -> Optional[str]:
        """Return ``handler.handler`` when the root wrapper reexports this module."""
        wrapper = self.project_root / "handler.py"
        selected = Path(code_file)
        if not selected.is_absolute():
            selected = self.project_root / selected

        try:
            if not wrapper.exists() or wrapper.resolve() == selected.resolve():
                return None
        except Exception:
            return None

        return "handler.handler" if self._root_wrapper_exports(wrapper, module) else None

    def _module_name_from_code_file(self, code_file: str) -> str:
        """Return the Python module path for *code_file* from the project root.

        SAM packages from ``CodeUri``.  Because this deployer uses the project
        root as ``CodeUri``, nested files must keep their package path in the
        handler, e.g. ``lambda_etl/handler.py`` -> ``lambda_etl.handler``.
        """
        path = Path(code_file)
        if not path.is_absolute():
            path = self.project_root / path

        return self._module_name_from_path(path, self.project_root)

    def _collect_runtime(self, existing_config: Optional[Dict[str, Any]]) -> str:
        """Select the Lambda runtime."""
        runtimes = [
            "python3.13", "python3.12", "python3.11", "python3.10", "python3.9", "python3.8",
            "nodejs20.x", "nodejs18.x", "nodejs16.x",
            "java17", "java11", "java8.al2",
            "dotnet8", "dotnet6",
            "go1.x",
            "ruby3.2", "ruby2.7",
        ]
        current = existing_config["runtime"] if existing_config else DEFAULT_RUNTIME
        print(f"\n🐍 Runtime atual: {current}")
        if not _ask_yes_no("   Deseja alterar?", False):
            return current

        print("\n📋 Runtimes disponíveis:")
        for i, rt in enumerate(runtimes, 1):
            print(f"  {i}. {rt}")

        while True:
            choice = input(f"\nEscolha o runtime (número ou nome) [{current}]: ").strip()
            if not choice:
                return current
            if choice.isdigit():
                idx = int(choice) - 1
                if 0 <= idx < len(runtimes):
                    return runtimes[idx]
            elif choice in runtimes:
                return choice
            print("⚠ Opção inválida!")

    def _collect_architecture(self, saved_default: str = "x86_64") -> str:
        """Select x86_64 or arm64.

        Parameters
        ----------
        saved_default:
            Previously persisted architecture value used as the pre-selected option.
        """
        arch_options = {"1": "x86_64", "2": "arm64"}
        default_num = "2" if saved_default == "arm64" else "1"
        print("\n🏗️  Arquitetura:")
        print("  1. x86_64 (Intel/AMD)")
        print("  2. arm64 (Graviton2)")
        choice = input(f"\nEscolha a arquitetura [{default_num}]: ").strip()
        return arch_options.get(choice, saved_default)

    def _collect_function_url(
        self, saved: Optional[Dict[str, Any]] = None
    ) -> Optional[Dict[str, Any]]:
        """Optionally configure a Lambda Function URL.

        Parameters
        ----------
        saved:
            Previously persisted Function URL config dict (e.g.
            ``{"AuthType": "NONE", "Cors": {...}}``).  Used as default
            so the user only needs to press Enter to keep existing settings.
        """
        has_saved = bool(saved)
        if has_saved:
            print(f"\n🌐 URL pública configurada (Auth: {saved.get('AuthType', 'NONE')})")

        if not _ask_yes_no("\n🌐 Deseja expor a função com URL pública?", has_saved):
            return None

        saved_auth = (saved or {}).get("AuthType", "NONE")
        default_no_auth = saved_auth != "AWS_IAM"
        auth_type = "NONE" if _ask_yes_no("   Permitir acesso sem autenticação?", default_no_auth) else "AWS_IAM"

        saved_cors = (saved or {}).get("Cors") or {}
        has_saved_cors = bool(saved_cors)
        cors_config = None
        if _ask_yes_no("   Configurar CORS?", has_saved_cors):
            def _join_list(val: Any) -> str:
                return ", ".join(val) if isinstance(val, list) else (str(val) if val else "*")

            default_origins = _join_list(saved_cors.get("AllowOrigins", ["*"]))
            default_methods = _join_list(saved_cors.get("AllowMethods", ["*"]))
            default_headers = _join_list(saved_cors.get("AllowHeaders", ["*"]))
            default_max_age = str(saved_cors.get("MaxAge", 300))

            origins_input = input(f"   Origens permitidas (separadas por vírgula) [{default_origins}]: ").strip() or default_origins
            methods_input = input(f"   Métodos permitidos (separados por vírgula) [{default_methods}]: ").strip() or default_methods
            headers_input = input(f"   Headers permitidos (separados por vírgula) [{default_headers}]: ").strip() or default_headers
            max_age_raw = input(f"   Max Age (segundos) [{default_max_age}]: ").strip() or default_max_age
            max_age = int(max_age_raw) if str(max_age_raw).isdigit() else 300

            cors_config = {
                "AllowOrigins": ["*"] if origins_input.strip() == "*" else [o.strip() for o in origins_input.split(",")],
                "AllowMethods": ["*"] if methods_input.strip() == "*" else [m.strip() for m in methods_input.split(",")],
                "AllowHeaders": ["*"] if headers_input.strip() == "*" else [h.strip() for h in headers_input.split(",")],
                "MaxAge": max_age,
            }

        return {"AuthType": auth_type, "Cors": cors_config}

    def _collect_env_vars(self, existing: Optional[Dict[str, str]] = None) -> Dict[str, str]:
        """Collect environment variables, optionally importing existing ones."""
        env_vars: Dict[str, str] = {}

        if existing:
            print("\n🔧 Variáveis de ambiente existentes:")
            for k, v in existing.items():
                print(f"   {k}={v}")
            if _ask_yes_no("   Deseja manter estas variáveis?", True):
                env_vars = dict(existing)

        if not _ask_yes_no("\n🔧 Deseja adicionar variáveis de ambiente?", False):
            return env_vars

        print("   Digite as variáveis no formato CHAVE=VALOR (linha vazia para finalizar)")
        while True:
            var = input("   Variável: ").strip()
            if not var:
                break
            if "=" in var:
                key, value = var.split("=", 1)
                env_vars[key.strip()] = value.strip()
            else:
                print("   ⚠ Formato inválido! Use CHAVE=VALOR")

        return env_vars

    def _collect_execution_role(
        self, function_name: str, saved_role_arn: Optional[str] = None
    ) -> Optional[str]:
        """Interactively select or create an IAM execution role for Lambda.

        Parameters
        ----------
        function_name:
            Used to generate a suggested role name when creating a new one.
        saved_role_arn:
            ARN from a previous run; displayed as the pre-selected option so
            the user can accept it with Enter.
        """
        print("\n" + "-" * 60)
        print("🔐 PAPEL DE EXECUÇÃO (IAM ROLE)")
        print("-" * 60)

        if saved_role_arn:
            print(f"\n💾 Role salva: {saved_role_arn}")
            if _ask_yes_no("   Deseja manter esta role?", True):
                return saved_role_arn

        if self._iam:
            print("\n🔍 Buscando roles existentes...")
            roles = self._iam.list_lambda_roles()

            if roles:
                print(f"\n✓ {len(roles)} role(s) de Lambda encontrada(s):\n")
                for i, role in enumerate(roles[:10], 1):
                    desc = role["description"]
                    if len(desc) > 50:
                        desc = desc[:47] + "..."
                    print(f"  {i}. {role['name']}")
                    print(f"     {desc}\n")
                if len(roles) > 10:
                    print(f"  ... e mais {len(roles) - 10} role(s)")

                print("-" * 60)
                print("\n💡 Digite o número, o nome completo, um ARN, ou Enter para criar nova role")
                choice = input("\nEscolha uma opção: ").strip()

                if choice.isdigit():
                    idx = int(choice) - 1
                    if 0 <= idx < len(roles):
                        print(f"✓ Usando role: {roles[idx]['name']}")
                        return roles[idx]["arn"]

                if choice and not choice.startswith("arn:"):
                    arn = self._iam.get_role_arn(choice)
                    if arn:
                        print(f"✓ Role encontrada: {choice}")
                        return arn
                    print(f"⚠ Role '{choice}' não encontrada")

                if choice and re.match(r"^arn:aws:iam::\d{12}:role/.+", choice):
                    print("✓ Usando ARN fornecido")
                    return choice

        # Create a new role
        print("\n📝 Criar nova role de execução\n")
        iam_available = bool(self._iam)
        print("Escolha uma opção:")
        print(f"  1. Criar role básica (apenas CloudWatch Logs){'' if iam_available else ' — indisponível'}")
        print(f"  2. Criar role a partir de um arquivo JSON de política{'' if iam_available else ' — indisponível'}")
        print(f"  3. Criar role a partir de JSON inline{'' if iam_available else ' — indisponível'}")
        print("  4. Informar ARN de role existente manualmente")
        option = input("\nOpção [1]: ").strip() or "1"

        if option in ("1", "2", "3") and not iam_available:
            print("⚠ IAM não disponível. Use a opção 4 para fornecer um ARN manualmente.")
            return None

        if option == "1":
            role_name = _ask("\nNome da role", f"{function_name}-execution-role")
            return self._iam.create_role(  # type: ignore[union-attr]
                role_name,
                description="Role de execução para Lambda - criada por SAM Deployer",
            )

        if option == "2":
            policy_file = _ask("\nCaminho do arquivo JSON com a política", "iam_policy.json")
            policy_path = Path(policy_file)
            if not policy_path.exists():
                print(f"⚠ Arquivo não encontrado: {policy_file}")
                if _ask_yes_no("Deseja criar um arquivo de exemplo?", True):
                    example = {
                        "Version": "2012-10-17",
                        "Statement": [
                            {"Effect": "Allow", "Action": ["s3:GetObject", "s3:PutObject"], "Resource": "arn:aws:s3:::meu-bucket/*"},
                            {"Effect": "Allow", "Action": ["dynamodb:GetItem", "dynamodb:PutItem", "dynamodb:Query"], "Resource": "arn:aws:dynamodb:*:*:table/MinhaTabela"},
                        ],
                    }
                    policy_path.write_text(json.dumps(example, indent=2))
                    print(f"✓ Arquivo de exemplo criado: {policy_file}")
                    print("  Edite o arquivo e execute novamente")
                return None
            try:
                policy_document = json.loads(policy_path.read_text(encoding="utf-8"))
            except json.JSONDecodeError as exc:
                print(f"✗ Erro ao ler JSON: {exc}")
                return None
            role_name = _ask("\nNome da role", f"{function_name}-execution-role")
            return self._iam.create_role(  # type: ignore[union-attr]
                role_name,
                description="Role customizada para Lambda - criada por SAM Deployer",
                inline_policy=policy_document,
            )

        if option == "3":
            print("\nCole o JSON da política (Ctrl+D para finalizar):")
            lines: List[str] = []
            try:
                while True:
                    lines.append(input())
            except EOFError:
                pass
            try:
                policy_document = json.loads("\n".join(lines))
            except json.JSONDecodeError as exc:
                print(f"✗ Erro ao parsear JSON: {exc}")
                return None
            role_name = _ask("\nNome da role", f"{function_name}-execution-role")
            return self._iam.create_role(  # type: ignore[union-attr]
                role_name,
                description="Role customizada para Lambda - criada por SAM Deployer",
                inline_policy=policy_document,
            )

        if option == "4":
            arn = _ask("\nARN da role", "arn:aws:iam::123456789012:role/lambda-execution-role")
            if re.match(r"^arn:aws:iam::\d{12}:role/.+", arn):
                return arn
            print("⚠ ARN inválido! Formato esperado: arn:aws:iam::<account-id>:role/<nome>")
            return None

        return None

    # ── full config collection ─────────────────────────────────────────────

    def collect_lambda_config(self) -> Dict[str, Any]:
        """Collect all Lambda configuration from the user.

        When a saved configuration exists the user can reuse it directly or
        override individual fields.  AWS-imported values are used as defaults
        when a function with the given name already exists.

        Returns a fully populated config dict ready for template generation.
        """
        print("\n" + "=" * 60)
        print("🚀 CONFIGURAÇÃO DA FUNÇÃO LAMBDA")
        print("=" * 60)

        saved_config = self._config_store.load()

        if saved_config:
            self._display_saved_config(saved_config)
            if _ask_yes_no("\n💾 Deseja usar esta configuração salva?", True):
                if not _ask_yes_no("   Deseja editar algum campo?", False):
                    self._using_saved_config = True
                    print("✓ Usando configuração salva")
                    if self._aws:
                        existing = self._get_existing_lambda_config(saved_config["function_name"])
                        if existing:
                            print(f"\n✓ Função Lambda '{saved_config['function_name']}' encontrada na AWS")
                            return self._merge_configs(saved_config, existing)
                    return saved_config
                print("   💡 Você pode alterar os valores sugeridos\n")

        # Collect each field; saved values are used as defaults so the user
        # only presses Enter to keep everything from the previous run.
        function_name = self._collect_function_name(saved_config)

        # Fetch live Lambda config once and reuse throughout (avoids duplicate API calls).
        existing_config: Optional[Dict[str, Any]] = None
        if self._aws:
            print(f"\n🔍 Verificando se a função '{function_name}' já existe na AWS...")
            existing_config = self._get_existing_lambda_config(function_name)
            if existing_config:
                print("✓ Função encontrada na AWS!")
                print(f"  Runtime: {existing_config['runtime']}")
                print(f"  Memória: {existing_config['memory']} MB")
                print(f"  Timeout: {existing_config['timeout']}s")
                print(f"  Handler: {existing_config['handler']}")
                print("  💡 Os valores ao vivo serão usados como padrão onde aplicável.")

        code_file = self._collect_code_file(saved_config)
        if code_file is None:
            print("❌ Operação cancelada: nenhum arquivo de código selecionado.")
            sys.exit(1)
        if not self._explicit_target:
            sam_root = self._resolve_sam_root(code_file)
            self._set_sam_root(sam_root)
        code_file = self._normalise_code_file_for_sam_root(code_file)
        print(f"✓ Diretório SAM/Lambda: {self.project_root}")
        handler = self._collect_handler(code_file, existing_config)
        runtime = self._collect_runtime(existing_config)

        # Architecture: prefer live Lambda > saved config > default
        saved_arch = (saved_config or {}).get("architecture", "x86_64")
        architecture = self._collect_architecture(saved_default=saved_arch)

        # Memory / Timeout: prefer live Lambda > saved config > built-in default
        default_memory = str(
            existing_config["memory"] if existing_config
            else (saved_config or {}).get("memory", DEFAULT_MEMORY_MB)
        )
        memory = _ask("\n💾 Memória (MB)", default_memory, _validate_memory)

        default_timeout = str(
            existing_config["timeout"] if existing_config
            else (saved_config or {}).get("timeout", DEFAULT_TIMEOUT_S)
        )
        timeout = _ask("\n⏱️  Timeout (segundos)", default_timeout, _validate_timeout)

        default_desc = (
            existing_config.get("description", "") if existing_config
            else (saved_config or {}).get("description", "")
        )
        description = _ask(
            "\n📋 Descrição da função",
            default_desc or "Função Lambda gerenciada pelo SAM Deployer",
        )

        # Function URL: show saved config so user can just press Enter to keep it
        function_url_config = self._collect_function_url(
            saved=(saved_config or {}).get("function_url")
        )

        # Environment variables: prefer live Lambda > saved config
        env_source = (
            existing_config.get("environment") if existing_config
            else (saved_config or {}).get("environment")
        )
        env_vars = self._collect_env_vars(env_source)

        # Layers: show saved layers so the user can keep, replace or extend them.
        saved_layers: List[str] = (saved_config or {}).get("layers") or []
        layers: List[str] = []
        if saved_layers:
            print("\n📦 Layers salvos:")
            for lyr in saved_layers:
                print(f"   - {lyr}")
            if _ask_yes_no("   Deseja manter estes layers?", True):
                layers = list(saved_layers)
            if _ask_yes_no("   Deseja adicionar novos layers?", False):
                print("   Digite os ARNs dos layers (linha vazia para finalizar)")
                while True:
                    layer = input("   Layer ARN: ").strip()
                    if not layer:
                        break
                    layers.append(layer)
        else:
            if _ask_yes_no("\n📦 Deseja adicionar Layers?", False):
                print("   Digite os ARNs dos layers (linha vazia para finalizar)")
                while True:
                    layer = input("   Layer ARN: ").strip()
                    if not layer:
                        break
                    layers.append(layer)

        # Execution role: offer saved ARN so user can keep it with Enter
        saved_role = (saved_config or {}).get("execution_role")
        execution_role = self._collect_execution_role(function_name, saved_role_arn=saved_role)
        if not execution_role:
            print("\n⚠ Nenhuma role de execução foi configurada")
            if not _ask_yes_no("Deseja continuar sem especificar uma role?", False):
                print("❌ Operação cancelada")
                sys.exit(1)
            print("💡 SAM criará uma role automaticamente durante o deploy")

        result: Dict[str, Any] = {
            "function_name": function_name,
            "handler": handler,
            "code_file": code_file,
            "sam_root": str(self.project_root),
            "runtime": runtime,
            "architecture": architecture,
            "memory": int(memory),
            "timeout": int(timeout),
            "description": description,
            "function_url": function_url_config,
            "environment": env_vars,
            "layers": layers,
            "execution_role": execution_role,
        }

        # Preserve the CloudFront link from the previous run so step 6 of run()
        # doesn't overwrite it before the post-deploy CloudFront setup runs.
        if saved_config and saved_config.get("function_url_link"):
            result["function_url_link"] = saved_config["function_url_link"]

        return result

    # ── config display / merge helpers ─────────────────────────────────────

    @staticmethod
    def _display_saved_config(config: Dict[str, Any]) -> None:
        print("\n📋 Configuração encontrada:")
        print("-" * 60)
        print(f"  Perfil AWS: {config.get('aws_profile', 'N/A')}")
        print(f"  Nome da Função: {config.get('function_name', 'N/A')}")
        print(f"  Arquivo: {config.get('code_file', 'N/A')}")
        print(f"  Diretório SAM: {config.get('sam_root', 'N/A')}")
        print(f"  Handler: {config.get('handler', 'N/A')}")
        print(f"  Runtime: {config.get('runtime', 'N/A')}")
        print(f"  Arquitetura: {config.get('architecture', 'N/A')}")
        print(f"  Memória: {config.get('memory', 'N/A')} MB")
        print(f"  Timeout: {config.get('timeout', 'N/A')}s")
        role = config.get("execution_role", "")
        if role:
            print(f"  Role de Execução: {role[:57] + '...' if len(role) > 60 else role}")
        else:
            print("  Role de Execução: Automática (SAM)")
        if config.get("function_url"):
            print("  URL Pública: Sim")
        if config.get("function_url_link"):
            link = config["function_url_link"]
            print(f"  Link CloudFront: {link.get('url') or link.get('domain', 'Configurado')}")
        if config.get("environment"):
            print(f"  Variáveis de Ambiente: {len(config['environment'])} variável(is)")
        print("-" * 60)

    @staticmethod
    def _merge_configs(
        saved: Dict[str, Any], existing_lambda: Optional[Dict[str, Any]]
    ) -> Dict[str, Any]:
        """Merge saved config with live Lambda config (live values take priority)."""
        if not existing_lambda:
            return saved
        live_layers = [layer.get("Arn", layer) for layer in existing_lambda.get("layers", [])]
        return {
            **saved,
            "runtime": existing_lambda.get("runtime", saved.get("runtime")),
            "memory": existing_lambda.get("memory", saved.get("memory")),
            "timeout": existing_lambda.get("timeout", saved.get("timeout")),
            "handler": existing_lambda.get("handler", saved.get("handler")),
            "environment": existing_lambda.get("environment", saved.get("environment", {})),
            "description": existing_lambda.get("description", saved.get("description", "")),
            "layers": live_layers or saved.get("layers", []),
        }

    # ── SAM file generation ────────────────────────────────────────────────

    @staticmethod
    def _sanitize_logical_id(name: str) -> str:
        """Convert *name* to a valid CloudFormation Logical ID (alphanumeric only)."""
        camel = "".join(w.capitalize() for w in name.replace("_", " ").replace("-", " ").split())
        sanitized = re.sub(r"[^a-zA-Z0-9]", "", camel)
        if sanitized and not sanitized[0].isalpha():
            sanitized = "Fn" + sanitized
        return sanitized or "LambdaFunction"

    def create_sam_template(self, config: Dict[str, Any]) -> Dict[str, Any]:
        """Build (or update) the SAM ``template.yaml`` structure."""
        if self.template_file.exists():
            print(f"\n📄 Arquivo template.yaml encontrado")
            template = yaml.safe_load(self.template_file.read_text(encoding="utf-8")) or {}
        else:
            template = {
                "AWSTemplateFormatVersion": "2010-09-09",
                "Transform": "AWS::Serverless-2016-10-31",
                "Description": "SAM Template para funções Lambda",
                "Globals": {"Function": {"Timeout": DEFAULT_TIMEOUT_S, "MemorySize": DEFAULT_MEMORY_MB}},
                "Resources": {},
            }

        function_config: Dict[str, Any] = {
            "Type": "AWS::Serverless::Function",
            "Properties": {
                "FunctionName": config["function_name"],
                "CodeUri": ".",
                "Handler": config["handler"],
                "Runtime": config["runtime"],
                "Architectures": [config["architecture"]],
                "MemorySize": config["memory"],
                "Timeout": config["timeout"],
                "Description": config["description"],
            },
        }

        if config["environment"]:
            function_config["Properties"]["Environment"] = {"Variables": config["environment"]}
        if config["layers"]:
            function_config["Properties"]["Layers"] = config["layers"]
        if config.get("execution_role"):
            function_config["Properties"]["Role"] = config["execution_role"]
            print(f"  ✓ Role de execução configurada: {config['execution_role']}")
        if config["function_url"]:
            url_cfg: Dict[str, Any] = {"AuthType": config["function_url"]["AuthType"]}
            if config["function_url"].get("Cors"):
                url_cfg["Cors"] = config["function_url"]["Cors"]
            function_config["Properties"]["FunctionUrlConfig"] = url_cfg

        logical_id = self._sanitize_logical_id(config["function_name"])
        if logical_id != config["function_name"]:
            print(f"  💡 ID lógico sanitizado: '{config['function_name']}' → '{logical_id}'")
        template["Resources"][logical_id] = function_config
        return template

    def save_sam_template(self, template: Dict[str, Any]) -> None:
        """Write *template* to ``template.yaml``."""
        print(f"\n💾 Salvando template em {self.template_file}")
        self.template_file.parent.mkdir(parents=True, exist_ok=True)
        self.template_file.write_text(
            yaml.dump(template, default_flow_style=False, sort_keys=False, allow_unicode=True),
            encoding="utf-8",
        )
        print("✓ Template salvo com sucesso!")

    def _collect_deploy_params(self, config: Dict[str, Any]) -> None:
        """Collect CloudFormation stack name, region, and S3 settings into config.

        Skips all prompts when ``_using_saved_config`` is True — values already in config.
        """
        if self._using_saved_config:
            config.setdefault("stack_name", f"{config['function_name']}-stack".replace("_", "-"))
            config.setdefault("region", self.aws_region or "sa-east-1")
            config.setdefault("confirm_changeset", False)
            return

        print("\n" + "-" * 60)
        print("☁️  CONFIGURAÇÃO DO DEPLOY")
        print("-" * 60)

        suggested_stack = config.get("stack_name") or f"{config['function_name']}-stack".replace("_", "-")
        stack_name = _ask("Nome da stack CloudFormation", suggested_stack, _validate_stack_name)
        default_region = config.get("region") or self.aws_region or "sa-east-1"
        region = _ask("Região AWS", default_region)
        use_custom_bucket = _ask_yes_no("Deseja especificar um bucket S3 customizado?", False)
        confirm_changeset = _ask_yes_no("Confirmar mudanças antes do deploy?", False)

        config["stack_name"] = stack_name
        config["region"] = region
        config["confirm_changeset"] = confirm_changeset
        if use_custom_bucket:
            s3_bucket = _ask(
                "Nome do bucket S3 para artefatos",
                config.get("s3_bucket", f"sam-artifacts-{region}"),
            )
            config["s3_bucket"] = s3_bucket
        else:
            config.pop("s3_bucket", None)

    def create_samconfig(self, config: Dict[str, Any]) -> None:
        """Write ``samconfig.toml`` using values already collected in *config*."""
        print("\n📝 Configurando samconfig.toml")

        stack_name = config.get("stack_name") or f"{config['function_name']}-stack".replace("_", "-")
        region = config.get("region") or self.aws_region or "sa-east-1"
        confirm_changeset = config.get("confirm_changeset", False)
        s3_bucket = config.get("s3_bucket")

        bucket_line = f's3_bucket = "{s3_bucket}"\n' if s3_bucket else "resolve_s3 = true\n"
        profile_line = f'profile = "{self.aws_profile}"\n' if self.aws_profile else ""
        content = (
            f'version = 0.1\n\n'
            f'[default]\n'
            f'[default.global.parameters]\n'
            f'stack_name = "{stack_name}"\n\n'
            f'[default.build.parameters]\n'
            f'cached = true\n'
            f'parallel = true\n\n'
            f'[default.validate.parameters]\n'
            f'lint = true\n\n'
            f'[default.deploy.parameters]\n'
            f'capabilities = "CAPABILITY_IAM"\n'
            f'confirm_changeset = {str(confirm_changeset).lower()}\n'
            f'{bucket_line}'
            f'region = "{region}"\n'
            f'{profile_line}'
        )
        self.samconfig_file.write_text(content)
        print(f"✓ samconfig.toml criado/atualizado!  Stack: {stack_name}  |  Região: {region}")
        if not s3_bucket:
            print("  💡 SAM criará automaticamente um bucket S3 para artefatos")

    # ── project scaffolding ────────────────────────────────────────────────

    def create_requirements_file(self, code_file: str) -> None:
        """Create the SAM-visible ``requirements.txt`` if it does not exist."""
        # Keep dependencies beside CodeUri.  Since CodeUri is the project root,
        # a requirements file next to a nested handler would be ignored by SAM.
        req_file = self.project_root / "requirements.txt"
        if req_file.exists():
            return
        print(f"\n📦 Criando {req_file}")
        if _ask_yes_no("Deseja adicionar dependências?", False):
            print("Digite os pacotes (um por linha, linha vazia para finalizar):")
            packages: List[str] = []
            while True:
                pkg = input("  Pacote: ").strip()
                if not pkg:
                    break
                packages.append(pkg)
            req_file.write_text("\n".join(packages))
        else:
            req_file.touch()
        print(f"✓ {req_file} criado!")

    def create_gitignore(self) -> None:
        """Create or update ``.gitignore`` with SAM/Python/IDE patterns."""
        gi = self.project_root / ".gitignore"
        content = (
            "# SAM\n.aws-sam/\nsamconfig.toml\n.lambda_deployer_config.json\ncloudfront_back/\n\n"
            "# Python\n__pycache__/\n*.py[cod]\n*$py.class\n*.so\n.Python\nenv/\nvenv/\nENV/\n.venv\n\n"
            "# IDEs\n.vscode/\n.idea/\n*.swp\n*.swo\n*~\n\n"
            "# OS\n.DS_Store\nThumbs.db\n\n"
            "# Logs\n*.log\n"
        )
        if not gi.exists():
            gi.write_text(content)
            print("✓ .gitignore criado!")
        else:
            existing = gi.read_text()
            if "cloudfront_back/" not in existing:
                with gi.open("a") as f:
                    f.write("\n# Backups CloudFront\ncloudfront_back/\n")
                print("✓ .gitignore atualizado!")

    def create_samignore(self) -> None:
        """Create or update ``.samignore`` to exclude dev files from the deploy package."""
        si = self.project_root / ".samignore"
        content = (
            "# Arquivos do deployer\nlambda_deployer.py\n.lambda_deployer_config.json\ncamedics_lambda_deployer/\ncloudfront_back/\n\n"
            "# Git\n.git/\n.gitignore\n\n"
            "# IDEs\n.vscode/\n.idea/\n*.swp\n*.swo\n\n"
            "# OS\n.DS_Store\nThumbs.db\n\n"
            "# Python cache\n__pycache__/\n*.pyc\n*.pyo\n*.pyd\n\n"
            "# Testes\ntest_*.py\n*_test.py\ntests/\n.pytest_cache/\n\n"
            "# Documentação\nREADME*.md\ndocs/\n\n"
            "# Logs\n*.log\n"
        )
        if not si.exists():
            si.write_text(content)
            print("✓ .samignore criado!")
        else:
            existing = si.read_text()
            additions = []
            if "lambda_deployer.py" not in existing:
                additions.append(
                    "# Arquivos do deployer\n"
                    "lambda_deployer.py\n"
                    ".lambda_deployer_config.json\n"
                )
            if "camedics_lambda_deployer/" not in existing:
                additions.append("# Deployer local\ncamedics_lambda_deployer/\n")
            if "cloudfront_back/" not in existing:
                additions.append("# Backups CloudFront\ncloudfront_back/\n")
            if additions:
                with si.open("a") as f:
                    f.write("\n" + "\n".join(additions))
                print("✓ .samignore atualizado!")

    # ── CloudFront link workflow ───────────────────────────────────────────

    def _prompt_link_config(
        self, config: Dict[str, Any], function_url: str = ""
    ) -> Optional[Dict[str, Any]]:
        """Ask the user how the Function URL should be exposed via CloudFront.

        *function_url* is optional — pass it only when already available (post-deploy).
        When omitted the summary shows a placeholder; the real URL is used at setup time.
        """
        saved_link = config.get("function_url_link")

        if saved_link and saved_link.get("domain") and saved_link.get("path_prefix"):
            print("\n🔗 Link CloudFront salvo:")
            print(f"  Domínio: {saved_link.get('domain')}")
            print(f"  Caminho: {saved_link.get('path_prefix')}")
            if saved_link.get("url"):
                print(f"  URL: {saved_link.get('url')}")
            if _ask_yes_no("Deseja usar esta configuração de link?", True):
                zone_id = saved_link.get("hosted_zone_id")
                if not zone_id and self._route53:
                    zone = self._route53.find_zone_for_domain(saved_link["domain"])
                    if zone:
                        zone_id = zone["id"]
                        saved_link["hosted_zone_id"] = zone_id
                if zone_id:
                    return saved_link
                print("⚠ Não encontrei a hosted zone desse domínio salvo. Vamos configurar novamente.")

        if not _ask_yes_no(
            "\n🔗 Deseja criar/atualizar um link HTTPS para a Function URL via CloudFront?", False
        ):
            return None

        if config.get("function_url", {}).get("AuthType") == "AWS_IAM":
            print("\n⚠ A Function URL está com AuthType AWS_IAM.")
            print("   CloudFront não assina requisições IAM automaticamente; o link pode retornar 403.")
            if not _ask_yes_no("   Deseja continuar mesmo assim?", False):
                return None

        if not self._route53:
            print("⚠ Route 53 não disponível")
            return None

        zones = self._route53.list_public_zones()
        if not zones:
            print("⚠ Nenhuma hosted zone pública encontrada no Route 53.")
            return None

        print("\n🌎 Domínios disponíveis no Route 53:")
        print("-" * 60)
        for i, zone in enumerate(zones, 1):
            print(f"  {i:2d}. {zone['name']:<35} ({zone['record_count']} registros)")
        print("-" * 60)

        hosted_zone: Optional[Dict[str, Any]] = None
        while not hosted_zone:
            choice = input("Digite o número ou nome do domínio: ").strip()
            if choice.isdigit():
                idx = int(choice) - 1
                if 0 <= idx < len(zones):
                    hosted_zone = zones[idx]
            else:
                norm = choice.strip().rstrip(".").lower()
                hosted_zone = next((z for z in zones if z["name"] == norm), None)
            if not hosted_zone:
                print("⚠ Domínio inválido. Escolha um item da lista.")

        default_sub = saved_link.get("subdomain", "api") if saved_link else "api"
        subdomain = _ask("Subdomínio", default_sub, _validate_subdomain)
        domain = (
            hosted_zone["name"]
            if subdomain.strip().strip(".") in ("", "@")
            else f"{subdomain.strip().strip('.')}.{hosted_zone['name']}"
        )
        print(f"\n✓ Domínio escolhido: {domain}")

        cors = config.get("function_url", {}).get("Cors") or {}
        allow_origins = cors.get("AllowOrigins") or []
        if allow_origins and "*" not in allow_origins and f"https://{domain}" not in allow_origins:
            print("⚠ CORS da Function URL não inclui este domínio.")
            print(f"  Origens atuais: {', '.join(allow_origins)}")
            print(f"  Considere incluir: https://{domain}")

        existing_dist = self._cf.find_distribution_by_alias(domain) if self._cf else None
        if existing_dist:
            print(f"✓ Já existe CloudFront para {domain}: {existing_dist['Id']}")
            if self._cf:
                cfg, _ = self._cf.get_distribution_config(existing_dist["Id"])
                if cfg:
                    self._cf.show_summary(existing_dist["Id"], cfg)
        else:
            print(f"✓ Nenhum CloudFront existente usa o alias {domain}")

        default_path = (
            saved_link.get("path_prefix")
            if saved_link and saved_link.get("path_prefix")
            else "/" + re.sub(r"[^a-z0-9-]+", "-", config["function_name"].lower()).strip("-")
        )
        path_raw = _ask(
            "Caminho público no CloudFront (ex: /pessoal)",
            default_path,
            _validate_path_prefix,
        )
        path_prefix = path_raw.strip()
        if path_prefix and path_prefix != "/":
            path_prefix = "/" + path_prefix.strip("/")

        print("\nResumo do link:")
        if function_url:
            print(f"  Function URL: {function_url}")
        else:
            print("  Function URL: (será gerada após deploy)")
        final_url = f"https://{domain}{path_prefix if path_prefix != '/' else '/'}"
        print(f"  Link público: {final_url}")
        print("  Encaminhamento: CloudFront preserva o caminho recebido ao enviar para a Lambda.")

        if not _ask_yes_no("Confirmar criação/atualização do link?", True):
            return None

        return {
            "enabled": True,
            "hosted_zone_id": hosted_zone["id"],
            "base_domain": hosted_zone["name"],
            "subdomain": subdomain,
            "domain": domain,
            "path_prefix": path_prefix,
        }

    def _setup_cloudfront_link(
        self,
        link_config: Dict[str, Any],
        function_url: str,
        function_name: str,
    ) -> Optional[Dict[str, Any]]:
        """Create or update the CloudFront distribution and Route 53 DNS records."""
        if not (self._cf and self._route53 and self._acm):
            print("⚠ Clientes Route 53/CloudFront/ACM não estão disponíveis.")
            return None

        domain = link_config["domain"]
        hosted_zone_id = link_config["hosted_zone_id"]
        path_prefix = link_config["path_prefix"]
        backup_dir = self.project_root / "cloudfront_back"

        existing_dist = self._cf.find_distribution_by_alias(domain)

        if existing_dist:
            distribution_id = existing_dist["Id"]
            print(f"\n☁️  CloudFront existente encontrado para {domain}: {distribution_id}")
            dist_config, etag = self._cf.get_distribution_config(distribution_id)
            if not dist_config or not etag:
                return None

            vc = dist_config.get("ViewerCertificate", {})
            cert_arn = (
                link_config.get("certificate_arn")
                or vc.get("ACMCertificateArn")
                or vc.get("Certificate")
            )
            if cert_arn:
                link_config["certificate_arn"] = cert_arn

            self._cf.show_summary(distribution_id, dist_config)
            self._cf.backup_config(
                domain, distribution_id, dist_config, backup_dir,
                etag=etag, cloudfront_domain=existing_dist.get("DomainName"),
                reason="before-update",
            )

            origin_id, origin_changed = self._cf.ensure_origin(dist_config, function_url, function_name)
            behaviors_changed = self._cf.upsert_behaviors(
                dist_config, origin_id, path_prefix, is_new_distribution=False
            )
            if behaviors_changed is None:
                return None

            if origin_changed or behaviors_changed:
                print("\n☁️  Atualizando distribuição CloudFront preservando configurações existentes...")
                cloudfront_domain = self._cf.update_distribution(distribution_id, etag, dist_config)
                if not cloudfront_domain:
                    return None
                print("✓ Distribuição CloudFront atualizada")
            else:
                print("✓ CloudFront já estava configurado para este link")
                cloudfront_domain = self._cf.get_domain(distribution_id) or existing_dist.get("DomainName")
        else:
            print(f"\n☁️  Nenhum CloudFront encontrado para {domain}. Criando nova distribuição...")
            cert_arn = link_config.get("certificate_arn") or self._acm.ensure_certificate(
                domain, hosted_zone_id
            )
            if not cert_arn:
                return None
            link_config["certificate_arn"] = cert_arn

            if path_prefix != "/":
                print("   A nova distribuição também terá o default behavior apontando para esta Lambda.")

            new_cfg = self._cf.build_new_config(domain, function_url, cert_arn, function_name, path_prefix)
            resp = self._cf.create_distribution(new_cfg)
            if not resp:
                return None

            distribution_id = resp["Distribution"]["Id"]
            cloudfront_domain = resp["Distribution"]["DomainName"]
            print("✓ Distribuição CloudFront criada")
            print(f"  ID: {distribution_id}")
            print(f"  Domínio: {cloudfront_domain}")

            self._cf.backup_config(
                domain, distribution_id,
                resp["Distribution"]["DistributionConfig"],
                backup_dir, etag=resp.get("ETag"),
                cloudfront_domain=cloudfront_domain, reason="after-create",
            )

        if cloudfront_domain:
            self._route53.upsert_alias_records(domain, hosted_zone_id, cloudfront_domain)

        self._cf.wait_until_deployed(distribution_id)
        self._cf.verify_link(domain, path_prefix, distribution_id)

        final_url = f"https://{domain}{path_prefix if path_prefix != '/' else '/'}"
        link_config.update({
            "domain": domain,
            "path_prefix": path_prefix,
            "distribution_id": distribution_id,
            "cloudfront_domain": cloudfront_domain,
            "url": final_url,
        })
        return link_config

    def configure_function_url_cloudfront_link(
        self, config: Dict[str, Any], function_url: str
    ) -> None:
        """Apply the pre-collected CloudFront link config using the real *function_url*.

        If link config was not collected upfront (edge case), falls back to prompting.
        """
        link_config = config.get("function_url_link")
        if not link_config or not link_config.get("domain"):
            link_config = self._prompt_link_config(config, function_url)
        if not link_config:
            return

        updated_link = self._setup_cloudfront_link(link_config, function_url, config["function_name"])
        if not updated_link:
            print("⚠ Link CloudFront não foi concluído.")
            return

        config["function_url_link"] = updated_link
        self._config_store.save(config)
        print(f"\n🔗 Link configurado com sucesso:\n  {updated_link['url']}")

    # ── summary ────────────────────────────────────────────────────────────

    def show_summary(self, config: Dict[str, Any]) -> None:
        """Print a full deployment configuration summary."""
        print("\n" + "=" * 60)
        print("📊 RESUMO DA CONFIGURAÇÃO")
        print("=" * 60)
        print(f"Perfil AWS:       {config.get('aws_profile') or self.aws_profile or 'Cadeia padrão'}")
        print(f"Stack CF:         {config.get('stack_name') or '(definida no deploy)'}")
        print(f"Região:           {config.get('region') or self.aws_region or 'sa-east-1'}")
        print(f"Nome da Função:   {config['function_name']}")
        print(f"Diretório SAM:    {config.get('sam_root') or self.project_root}")
        print(f"Handler:          {config['handler']}")
        print(f"Runtime:          {config['runtime']}")
        print(f"Arquitetura:      {config['architecture']}")
        print(f"Memória:          {config['memory']} MB")
        print(f"Timeout:          {config['timeout']}s")
        print(f"Descrição:        {config['description']}")
        print(f"Role de Execução: {config.get('execution_role') or 'Automática (SAM criará)'}")
        if config["function_url"]:
            print(f"URL Pública:      Sim (Auth: {config['function_url']['AuthType']})")
        else:
            print("URL Pública:      Não")
        if config.get("function_url_link"):
            link = config["function_url_link"]
            domain = link.get("domain", "")
            path = link.get("path_prefix", "")
            final = f"https://{domain}{path}" if domain else link.get("url", "")
            print(f"Link CloudFront:  {final}")
        if config["environment"]:
            print("\nVariáveis de Ambiente:")
            for k, v in config["environment"].items():
                print(f"  {k}={v}")
        if config["layers"]:
            print("\nLayers:")
            for layer in config["layers"]:
                print(f"  - {layer}")
        print("=" * 60)

    # ── Mangum / CloudFront path-prefix check ─────────────────────────────

    def _check_mangum_base_path(self, config: Dict[str, Any]) -> None:
        """Warn and optionally patch handler.py when Mangum lacks api_gateway_base_path.

        Runs only when a CloudFront link with a non-root path prefix is configured,
        because without api_gateway_base_path Mangum forwards the full prefixed path
        to FastAPI (e.g. /financeiro-etl-api/docs instead of /docs), causing 404s.
        """
        link = config.get("function_url_link") or {}
        path_prefix = link.get("path_prefix", "")
        if not path_prefix or path_prefix == "/":
            return

        # Resolve handler file from "<module>.<function>" spec (e.g. "handler.handler")
        handler_spec = config.get("handler", "")
        if not handler_spec or "." not in handler_spec:
            return
        module_name = handler_spec.rsplit(".", 1)[0]
        handler_file = self.project_root / (module_name.replace(".", "/") + ".py")
        if not handler_file.exists():
            return

        source = handler_file.read_text(encoding="utf-8")
        if "Mangum" not in source:
            return

        try:
            tree = ast.parse(source)
        except SyntaxError as exc:
            print(f"⚠ Erro de sintaxe ao analisar {handler_file}: {exc}")
            return

        # Find real Mangum(...) calls only; ignore docstrings, comments and examples.
        mangum_calls = [
            node for node in ast.walk(tree)
            if self._is_named_call(node, "Mangum")
        ]
        if not mangum_calls:
            return
        missing = [
            call for call in mangum_calls
            if not self._call_has_keyword(call, "api_gateway_base_path")
        ]
        if not missing:
            print(f"\n✓ Mangum: api_gateway_base_path já configurado em {handler_file}")
            return

        print(f"\n{'=' * 60}")
        print("⚠  Mangum detectado + CloudFront com path prefix")
        print(f"{'=' * 60}")
        print(f"  Arquivo  : {handler_file}")
        print(f"  Prefix   : {path_prefix}")
        print()
        print("  Sem api_gateway_base_path o FastAPI recebe o path completo")
        print(f"  (ex: {path_prefix}/docs em vez de /docs) e retorna 404.")
        print()

        if not _ask_yes_no(
            f'  Adicionar api_gateway_base_path="{path_prefix}" ao Mangum?', True
        ):
            return

        new_source = self._insert_keyword_in_call_source(
            source,
            missing[0],
            "api_gateway_base_path",
            path_prefix,
        )
        if not new_source or new_source == source:
            print(f"  ⚠ Não foi possível modificar {handler_file} automaticamente.")
            print(f'    Adicione manualmente: api_gateway_base_path="{path_prefix}"')
            return

        handler_file.write_text(new_source, encoding="utf-8")
        print(f'\n  ✓ api_gateway_base_path="{path_prefix}" adicionado em {handler_file}')

    def _check_fastapi_root_path(self, config: Dict[str, Any]) -> None:
        """Warn and optionally patch FastAPI app when root_path is missing.

        Mangum always sets scope["root_path"]="" regardless of api_gateway_base_path.
        Without root_path on FastAPI(), the Swagger UI fetches /openapi.json (no prefix),
        which falls to the CloudFront DefaultCacheBehavior instead of the correct Lambda.

        Scans all .py files in project_root because FastAPI is often defined in a
        separate file (e.g. app.py) and only imported by the handler.
        """
        link = config.get("function_url_link") or {}
        path_prefix = link.get("path_prefix", "")
        if not path_prefix or path_prefix == "/":
            return

        for py_file in self.project_root.glob("*.py"):
            try:
                source = py_file.read_text(encoding="utf-8")
            except OSError:
                continue

            if "FastAPI(" not in source:
                continue

            try:
                tree = ast.parse(source)
            except SyntaxError as exc:
                print(f"⚠ Erro de sintaxe ao analisar {py_file}: {exc}")
                continue

            found_any = False
            missing = []
            for node in ast.walk(tree):
                if not self._is_named_call(node, "FastAPI"):
                    continue
                found_any = True
                if not self._call_has_keyword(node, "root_path"):
                    missing.append(node)

            if not found_any:
                continue

            if not missing:
                print(f"\n✓ FastAPI: root_path já configurado em {py_file.name}")
                continue

            print(f"\n{'=' * 60}")
            print("⚠  FastAPI detectado sem root_path + CloudFront com path prefix")
            print("=" * 60)
            print(f"  Arquivo  : {py_file}")
            print(f"  Prefix   : {path_prefix}")
            print()
            print("  Mangum seta scope['root_path']=\"\" para todas as requisições.")
            print("  Sem root_path, o Swagger UI busca /openapi.json sem prefixo,")
            print("  caindo no DefaultCacheBehavior em vez desta Lambda.")
            print()

            if not _ask_yes_no(
                f'  Adicionar root_path="{path_prefix}" ao FastAPI?', True
            ):
                continue

            new_source = self._insert_keyword_in_call_source(
                source,
                missing[0],
                "root_path",
                path_prefix,
            )
            if not new_source or new_source == source:
                print(f"  ⚠ Não foi possível modificar {py_file} automaticamente.")
                print(f'    Adicione manualmente: root_path="{path_prefix}"')
                continue

            py_file.write_text(new_source, encoding="utf-8")
            print(f'\n  ✓ root_path="{path_prefix}" adicionado em {py_file.name}')

    # ── main entry point ───────────────────────────────────────────────────

    def run(self) -> None:
        """Execute the full interactive deploy workflow.

        Phases
        ------
        1. COLLECT  — all questions asked upfront (Lambda, deploy params, link config).
        2. CONFIRM  — single summary shown, one confirmation prompt.
        3. EXECUTE  — generate files, sam build, sam deploy, CloudFront setup.
        """
        print("=" * 60)
        print("🚀 AWS LAMBDA DEPLOYER COM SAM")
        print("=" * 60)

        # ── PREREQS ──────────────────────────────────────────────────────────

        temp_sam = SAMRunner(self.project_root, AWSClientManager())
        if not temp_sam.is_installed():
            print("\n⚠ SAM CLI não está instalado!")
            print("Instale com: brew install aws-sam-cli")
            if not _ask_yes_no("Deseja continuar mesmo assim?", False):
                return

        # ── PHASE 1: COLLECT ─────────────────────────────────────────────────

        self.configure_aws_profile()
        self.initialise_managers()
        if self._sam is None:
            print("✗ SAM runner não pôde ser inicializado")
            return

        # Lambda config (name, handler, runtime, memory, role, env, layers, function_url)
        config = self.collect_lambda_config()
        self._apply_sam_root_from_config(config)
        if self.aws_profile:
            config["aws_profile"] = self.aws_profile
        if self.aws_region:
            config.setdefault("region", self.aws_region)

        # Deploy params (stack name, region, S3 bucket, changeset confirmation)
        self._collect_deploy_params(config)

        # CloudFront link (domain, subdomain, path) — only when function_url is enabled
        # and no saved link config already exists (re-runs reuse the saved one)
        if config.get("function_url") and not self._using_saved_config:
            saved_link = config.get("function_url_link")
            if not (saved_link and saved_link.get("domain")):
                link_config = self._prompt_link_config(config)
                if link_config:
                    config["function_url_link"] = link_config

        # ── PHASE 2: CONFIRM ─────────────────────────────────────────────────

        self.show_summary(config)
        if not _ask_yes_no("\n✅ Confirmar e iniciar deploy?", True):
            print("❌ Operação cancelada")
            return

        # Check Mangum api_gateway_base_path when CloudFront path prefix is configured
        self._check_mangum_base_path(config)
        self._check_fastapi_root_path(config)

        # Persist full config (including stack_name, link_config, etc.)
        self._config_store.save(config)

        # ── PHASE 3: EXECUTE ─────────────────────────────────────────────────

        template = self.create_sam_template(config)
        self.save_sam_template(template)
        self.create_samconfig(config)
        self.create_requirements_file(config["code_file"])
        self.create_gitignore()
        self.create_samignore()
        print("\n✓ Todos os arquivos necessários foram criados/atualizados!")

        build_success = self._sam.build()

        if build_success and self._sam.deploy():
            if config.get("function_url") and self._aws:
                url = self._wait_for_function_url(config["function_name"])
                if url:
                    print(f"\n🌐 Function URL: {url}")
                    if config.get("function_url_link"):
                        self.configure_function_url_cloudfront_link(config, url)
                else:
                    print("\n⚠ Function URL não ficou disponível para configurar link CloudFront.")

        # 10. Done
        print("\n" + "=" * 60)
        print("✅ PROCESSO CONCLUÍDO!")
        print("=" * 60)
        print("\nPróximos passos:")
        print("  1. sam build       - Para construir a aplicação")
        print("  2. sam deploy      - Para fazer deploy")
        print("  3. sam logs        - Para ver os logs")
        print("  4. sam delete      - Para remover a stack")
        print("=" * 60)


# ---------------------------------------------------------------------------
# Entry point
# ---------------------------------------------------------------------------

def main() -> None:
    """Script entry point.

    Usage::

        python lambda_deployer.py                     # deploy no diretório atual
        python lambda_deployer.py /path/to/project    # aponta para outro diretório
        python lambda_deployer.py --target /path      # forma explícita
    """
    parser = argparse.ArgumentParser(
        description="Deployer interativo de AWS Lambda via SAM",
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )
    parser.add_argument(
        "target",
        nargs="?",
        metavar="TARGET_DIR",
        help="Diretório do projeto Lambda a fazer deploy (padrão: diretório atual)",
    )
    parser.add_argument(
        "--target",
        dest="target_flag",
        metavar="TARGET_DIR",
        help="Alternativa explícita ao argumento posicional",
    )
    args = parser.parse_args()

    raw_target = args.target_flag or args.target
    target_dir: Optional[Path] = None
    if raw_target:
        target_dir = Path(raw_target).expanduser().resolve()
        if not target_dir.is_dir():
            print(f"❌ Diretório alvo não encontrado: {target_dir}")
            sys.exit(1)
        print(f"📁 Diretório alvo: {target_dir}")

    try:
        LambdaDeployer(target_dir).run()
    except KeyboardInterrupt:
        print("\n\n❌ Operação cancelada pelo usuário")
        sys.exit(1)
    except Exception as exc:
        print(f"\n\n❌ Erro inesperado: {exc}")
        traceback.print_exc()
        sys.exit(1)


if __name__ == "__main__":
    main()
