"""Offline official SAM transform plus CloudFormation schema validation."""

from __future__ import annotations

import json
import os
import subprocess
import sys
import tempfile
from pathlib import Path

from cfnlint.decode import decode
from samtranslator.translator.transform import transform


class OfflinePolicies:
    def load(self) -> dict[str, str]:
        # Terraform supplies the execution role; connector uses inline ENI permissions.
        return {}


def main() -> None:
    os.environ["AWS_DEFAULT_REGION"] = "us-west-2"
    os.environ["AWS_EC2_METADATA_DISABLED"] = "true"
    template, errors = decode("infra/app/web-function.yaml")
    if errors:
        raise ValueError(errors)
    expanded = transform(template, {}, OfflinePolicies())
    with tempfile.TemporaryDirectory(prefix="phase5-sam-") as directory:
        path = Path(directory) / "expanded.json"
        path.write_text(json.dumps(expanded))
        # cfn-lint's SAM attribute mapping lacks Web Function/connector attributes.
        # Validate actual expanded resources. W3005 is a redundant DependsOn generated
        # by SAM itself; every CloudFormation schema error still fails this command.
        subprocess.run(
            [
                str(Path(sys.executable).with_name("cfn-lint")),
                "-r",
                "us-west-2",
                "--non-zero-exit-code",
                "error",
                "-t",
                str(path),
                "infra/app/web-function-policy.yaml",
            ],
            check=True,
        )
    print("SAM transform and CloudFormation schemas valid (offline, us-west-2).")


if __name__ == "__main__":
    main()
