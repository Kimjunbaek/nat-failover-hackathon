"""서울 NAT 실습 전용. AWS CloudShell에서 python3 ~/nat-stop-test.py"""
import json
import time
from datetime import datetime, timezone
from pathlib import Path

import boto3
from botocore.exceptions import ClientError

OLD = "i-0000000000000d6c1"
TEST = "i-00000000000008bb2"
GROUP = "nat-ha-lab-nat-asg"
TABLE = "rtb-000000000000028e0"
session = boto3.Session(region_name="ap-northeast-2")
ec2 = session.client("ec2")
asg = session.client("autoscaling")
ssm = session.client("ssm")


def require(ok, message):
    if not ok:
        raise RuntimeError(message)


def group():
    return asg.describe_auto_scaling_groups(AutoScalingGroupNames=[GROUP])["AutoScalingGroups"][0]


def instance(i):
    return ec2.describe_instances(InstanceIds=[i])["Reservations"][0]["Instances"][0]


def routed_to(inst):
    routes = ec2.describe_route_tables(RouteTableIds=[TABLE])["RouteTables"][0]["Routes"]
    interfaces = {n["NetworkInterfaceId"] for n in inst["NetworkInterfaces"]}
    return any(r.get("DestinationCidrBlock") == "0.0.0.0/0" and r["State"] == "active"
               and (r.get("NetworkInterfaceId") in interfaces or r.get("InstanceId") == inst["InstanceId"])
               for r in routes)


def remote(command, timeout=65):
    cid = ssm.send_command(InstanceIds=[TEST], DocumentName="AWS-RunShellScript",
                           Parameters={"commands": [command]})["Command"]["CommandId"]
    until = time.monotonic() + timeout
    while time.monotonic() < until:
        time.sleep(2)
        try:
            result = ssm.get_command_invocation(CommandId=cid, InstanceId=TEST)
        except ssm.exceptions.InvocationDoesNotExist:
            continue
        if result["Status"] == "Success":
            return result["StandardOutputContent"]
        require(result["Status"] in ("Pending", "Delayed", "InProgress"),
                f"테스트 서버 측정 명령 실패: {result['Status']} {result['StandardErrorContent']}")
    raise TimeoutError("테스트 서버 SSM 응답 대기 시간 초과")


def main():
    require(session.client("sts").get_caller_identity()["Account"] == "123456789012", "계정 불일치")
    g = group()
    require(g["DesiredCapacity"] == 1 and len(g["Instances"]) == 1
            and g["Instances"][0]["InstanceId"] == OLD and g["Instances"][0]["LifecycleState"] == "InService",
            "예정된 기존 NAT와 현재 ASG 상태가 다릅니다. 중지하지 않습니다.")
    old, test = instance(OLD), instance(TEST)
    require(old["State"]["Name"] == "running" and routed_to(old), "기존 NAT 또는 기본 경로 이상")
    require(test["State"]["Name"] == "running" and not test.get("PublicIpAddress")
            and test["SubnetId"] == "subnet-00000000000009b48", "테스트 서버 구성 불일치")
    ip = old["PublicIpAddress"]
    run = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
    log = f"/var/log/nat-stop-{run}.log"
    print("테스트 서버에 독립 측정기 시작. 기존 NAT 출구 IP 3회 확인 중:", ip, flush=True)
    cmd = f"""set -eu
systemd-run --unit=nat-stop-{run} --property=RuntimeMaxSec=35min /bin/bash -c '
while true; do
  if ip=$(curl -4fsS --max-time 4 https://checkip.amazonaws.com); then
    result="OK $ip"
  else
    result=FAIL
  fi
  echo "$(date -u +%s) $result" >> {log}
  sleep 5
done'
for i in $(seq 1 20); do
  if [ "$(tail -n 3 {log} 2>/dev/null | grep -Fc ' OK {ip}' || true)" = 3 ]; then exit 0; fi
  sleep 2
done
exit 1"""
    remote(cmd)
    report = Path.home() / f"nat-stop-{run}.json"
    record = dict(old_instance=OLD, old_ip=ip, old_az=old["Placement"]["AvailabilityZone"],
                  test_instance=TEST, test_log=log, stop_request_epoch=int(time.time()))
    def save():
        report.write_text(json.dumps(record, ensure_ascii=False, indent=2), encoding="utf-8")
    save()
    print("사전 점검 통과. NAT 중지 요청:", OLD, "| 기록:", report, flush=True)
    ec2.stop_instances(InstanceIds=[OLD])
    deadline = time.monotonic() + 25 * 60
    while time.monotonic() < deadline:
        for member in group()["Instances"]:
            if member["InstanceId"] == OLD or member["LifecycleState"] != "InService":
                continue
            new = instance(member["InstanceId"])
            if not new.get("PublicIpAddress") or not routed_to(new):
                continue
            record.update(new_instance=new["InstanceId"], new_ip=new["PublicIpAddress"],
                          new_az=new["Placement"]["AvailabilityZone"], route_active=True)
            save()
            break
        if record.get("route_active"):
            break
        time.sleep(10)
    require(record.get("route_active"), "25분 내 대체 NAT 및 경로 확인 실패. ASG 활동과 새 NAT 시스템 로그를 확인하세요.")
    print("새 NAT와 활성 경로 확인:", record["new_instance"], record["new_ip"], flush=True)
    while time.monotonic() < deadline:
        try:
            output = remote(f"cat {log}", timeout=30)
        except (TimeoutError, ClientError):
            time.sleep(8)
            continue
        rows = [line.split() for line in output.splitlines() if line.strip()]
        samples = [r for r in rows if r[0].isdigit() and int(r[0]) >= record["stop_request_epoch"]]
        wanted = ["OK", record["new_ip"]]
        if len(samples) >= 3 and all(r[1:] == wanted for r in samples[-3:]):
            fails = [int(r[0]) for r in samples if r[1:] == ["FAIL"]]
            record.update(https_confirmed=True, failure_observed=bool(fails), samples=rows,
                          stop_to_confirmation_seconds=int(samples[-1][0])-record["stop_request_epoch"])
            if fails:
                record["first_fail_to_confirmation_seconds"] = int(samples[-1][0])-fails[0]
            save()
            print("HTTPS 3회 연속 성공. 실패 관측:", bool(fails),
                  "| 중지 요청부터 3회 성공 확인까지:", record["stop_to_confirmation_seconds"], "초")
            print("가용 영역:", record["old_az"], "→", record["new_az"], "| 결과 파일:", report)
            return
        time.sleep(6)
    raise TimeoutError(f"관측 시간 내 HTTPS 확인 미완료. 테스트 서버 로그: {log}")


if __name__ == "__main__":
    main()
