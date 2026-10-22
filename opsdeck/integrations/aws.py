"""AWS: EC2 instances, EKS clusters, CloudWatch alarms (read-only).

Credentials come from the standard AWS chain: AWS_PROFILE / SSO on a laptop,
the instance profile on EC2, or IRSA / EKS Pod Identity in Kubernetes.
Set AWS_REGIONS (comma-separated) to enable.
"""

from __future__ import annotations

from ..config import env, env_list
from ..registry import Column, Integration, OpError, Param, Section, read_op
from ._common import ago, iso


def regions() -> list[str]:
    return env_list("AWS_REGIONS")


def _region(region: str | None) -> str:
    allowed = regions() or ["us-east-1"]
    region = region or allowed[0]
    if regions() and region not in allowed:
        raise OpError(f"region '{region}' not in AWS_REGIONS", 403)
    return region


class AWS(Integration):
    name = "aws"
    title = "AWS"
    category = "Cloud"
    icon = "cloud"
    description = "EC2, EKS and CloudWatch alarms across configured regions."
    required_env = ("AWS_REGIONS",)

    def _client(self, service: str, region: str):
        import boto3
        from botocore.config import Config

        session = boto3.session.Session(profile_name=env("AWS_PROFILE"))
        return session.client(service, region_name=region, verify=self.settings.ca_bundle or None,
                              config=Config(retries={"max_attempts": 3, "mode": "standard"}, read_timeout=20))

    def health(self):
        ident = self._client("sts", _region(None)).get_caller_identity()
        return {"ok": True, "detail": f"{ident['Arn'].split(':')[-1]} in {ident['Account']}"}

    @read_op("CloudWatch alarms in a given state.", (
        Param("region", description="AWS region (default: first in AWS_REGIONS)"),
        Param("state", default="ALARM", enum=["ALARM", "INSUFFICIENT_DATA", "OK"]),
    ))
    def alarms(self, region=None, state="ALARM"):
        region = _region(region)
        cw = self._client("cloudwatch", region)
        rows = []
        for page in cw.get_paginator("describe_alarms").paginate(StateValue=state, AlarmTypes=["MetricAlarm", "CompositeAlarm"]):
            for kind in ("MetricAlarms", "CompositeAlarms"):
                for a in page.get(kind, []):
                    rows.append({"region": region, "name": a["AlarmName"], "state": a["StateValue"],
                                 "metric": a.get("MetricName") or "composite",
                                 "namespace": a.get("Namespace", ""), "since": iso(a.get("StateUpdatedTimestamp")),
                                 "reason": (a.get("StateReason") or "")[:300]})
        return sorted(rows, key=lambda r: r["since"] or "", reverse=True)

    @read_op("EC2 instances (optionally by state).", (
        Param("region"), Param("state", default="running",
                               enum=["running", "stopped", "pending", "stopping", "terminated", "all"]),
        Param("name_filter", description="Substring of the Name tag"),
    ))
    def ec2_instances(self, region=None, state="running", name_filter=None):
        region = _region(region)
        filters = [] if state == "all" else [{"Name": "instance-state-name", "Values": [state]}]
        if name_filter:
            filters.append({"Name": "tag:Name", "Values": [f"*{name_filter}*"]})
        ec2 = self._client("ec2", region)
        rows = []
        for page in ec2.get_paginator("describe_instances").paginate(Filters=filters):
            for res in page["Reservations"]:
                for i in res["Instances"]:
                    tags = {t["Key"]: t["Value"] for t in i.get("Tags", [])}
                    rows.append({"region": region, "name": tags.get("Name", ""), "id": i["InstanceId"],
                                 "type": i["InstanceType"], "state": i["State"]["Name"],
                                 "az": i["Placement"]["AvailabilityZone"], "private_ip": i.get("PrivateIpAddress"),
                                 "launched": iso(i.get("LaunchTime"))})
                    if len(rows) >= 500:
                        return rows
        return rows

    @read_op("EKS clusters with version and status.", (Param("region"),))
    def eks_clusters(self, region=None):
        region = _region(region)
        eks = self._client("eks", region)
        rows = []
        for name in eks.list_clusters().get("clusters", []):
            c = eks.describe_cluster(name=name)["cluster"]
            rows.append({"region": region, "name": name, "status": c["status"], "version": c["version"],
                         "platform": c.get("platformVersion"), "created": iso(c.get("createdAt")),
                         "endpoint_public": c.get("resourcesVpcConfig", {}).get("endpointPublicAccess")})
        return rows

    def widget(self):
        region = (regions() or ["us-east-1"])[0]
        return [
            Section("CloudWatch alarms", "alarms", inputs=["region", "state"],
                    params={"region": region, "state": "ALARM"},
                    columns=[Column("name", fmt="code"), Column("state", fmt="status"), Column("metric"),
                             Column("namespace"), Column("since", fmt="time"), Column("reason")],
                    refresh_secs=120),
            Section("EKS clusters", "eks_clusters", inputs=["region"], params={"region": region},
                    columns=[Column("name", fmt="code"), Column("status", fmt="status"), Column("version"),
                             Column("platform"), Column("endpoint_public"), Column("created", fmt="time")]),
            Section("EC2 instances", "ec2_instances", inputs=["region", "state", "name_filter"],
                    params={"region": region, "state": "running"},
                    columns=[Column("name"), Column("id", fmt="code"), Column("type"),
                             Column("state", fmt="status"), Column("az"), Column("private_ip", fmt="code"),
                             Column("launched", fmt="time")]),
        ]

    def attention(self):
        firing = []
        for r in regions() or ["us-east-1"]:
            firing += self.invoke("alarms", {"region": r, "state": "ALARM"})
        if not firing:
            return []
        return [{"severity": "critical", "title": f"{len(firing)} CloudWatch alarm(s) firing",
                 "detail": ", ".join(f"{a['name']} ({a['region']})" for a in firing[:6])}]

    # -------------------------------------------------------------- demo
    def demo_alarms(self, region=None, state="ALARM"):
        rows = [
            ("payments-settlement-lag-high", "ALARM", "SettlementLagSeconds", "Acme/Payments", 0.5,
             "Threshold Crossed: 3 datapoints [912.0, 870.0, 845.0] > 600.0"),
            ("checkout-alb-5xx-rate", "OK", "HTTPCode_Target_5XX_Count", "AWS/ApplicationELB", 2, ""),
            ("rds-ledger-cpu-high", "INSUFFICIENT_DATA", "CPUUtilization", "AWS/RDS", 30, "Insufficient data"),
        ]
        return [{"region": region or "us-east-1", "name": n, "state": s, "metric": m, "namespace": ns,
                 "since": ago(hours=h), "reason": rs} for n, s, m, ns, h, rs in rows if s == state]

    def demo_ec2_instances(self, region=None, state="running", name_filter=None):
        rows = [("bastion-prod", "i-0a12b34c56d78e901", "t3.small", "running", "us-east-1a", "10.0.0.10", 300),
                ("eks-prod-ng-general", "i-0b23c45d67e89f012", "m6i.xlarge", "running", "us-east-1a", "10.0.1.12", 20),
                ("eks-prod-ng-general", "i-0c34d56e78f90a123", "m6i.xlarge", "running", "us-east-1b", "10.0.2.40", 20),
                ("eks-prod-ng-general", "i-0d45e67f89a01b234", "m6i.xlarge", "running", "us-east-1c", "10.0.3.77", 20),
                ("legacy-reports", "i-0e56f78a90b12c345", "m5.large", "stopped", "us-east-1a", "10.0.0.55", 900)]
        return [{"region": region or "us-east-1", "name": n, "id": i, "type": t, "state": s, "az": az,
                 "private_ip": ip, "launched": ago(days=d)} for n, i, t, s, az, ip, d in rows
                if (state == "all" or s == state) and (not name_filter or name_filter in n)]

    def demo_eks_clusters(self, region=None):
        return [{"region": region or "us-east-1", "name": "prod-eks", "status": "ACTIVE", "version": "1.30",
                 "platform": "eks.12", "created": ago(days=600), "endpoint_public": True},
                {"region": region or "us-east-1", "name": "staging-eks", "status": "ACTIVE", "version": "1.31",
                 "platform": "eks.6", "created": ago(days=400), "endpoint_public": False}]
