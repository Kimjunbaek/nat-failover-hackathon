"""서울 NAT 실습 전용. AWS CloudShell에서 python3 ~/nat-stop-test.py"""
import argparse
import json
import time
from datetime import datetime, timezone
from pathlib import Path

import boto3
from botocore.exceptions import ClientError

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
    parser = argparse.ArgumentParser(description="NAT 촬영용 사전 점검과 장애 시험")
    parser.add_argument("mode", choices=["check", "run"])
    args = parser.parse_args()
    require(session.client("sts").get_caller_identity()["Account"] == "123456789012", "계정 불일치")
    g = group()
    require(g["MinSize"] == g["MaxSize"] == g["DesiredCapacity"] == 1 and len(g["Instances"]) == 1
            and g["Instances"][0]["LifecycleState"] == "InService" and g["Instances"][0]["HealthStatus"] == "Healthy",
            "ASG 최소·희망·최대 1 및 Healthy/InService NAT 한 대를 확인하세요.")
    old_id = g["Instances"][0]["InstanceId"]
    old, test = instance(old_id), instance(TEST)
    require(old_id != TEST and any(t["Key"] == "Role" and t["Value"] == "nat-ha-lab-nat" for t in old.get("Tags", [])), "NAT 태그 불일치")
    require(old["State"]["Name"] == "running" and routed_to(old), "기존 NAT 또는 기본 경로 이상")
    require(old.get("SourceDestCheck") is False, "NAT 출발지/대상 확인 설정을 확인하세요.")
    require(test["State"]["Name"] == "running" and not test.get("PublicIpAddress")
            and test["SubnetId"] == "subnet-00000000000009b48", "테스트 서버 구성 불일치")
    ip = old["PublicIpAddress"]
    nodes = ssm.describe_instance_information(Filters=[{"Key":"InstanceIds","Values":[TEST]}])["InstanceInformationList"]
    require(nodes and nodes[0]["PingStatus"] == "Online", "테스트 EC2의 SSM 상태가 Online인지 확인하세요.")
    print("현재 NAT:", old_id, "| AZ:", old["Placement"]["AvailabilityZone"], "| 출구 IP:", ip)
    print("프라이빗 기본 경로 정상 | 테스트 EC2 SSM Online:", TEST)
    if args.mode == "check":
        print("촬영 준비 점검 통과")
        return
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
    record = dict(old_instance=old_id, old_ip=ip, old_az=old["Placement"]["AvailabilityZone"],
                  test_instance=TEST, test_log=log, stop_request_epoch=int(time.time()))
    def save():
        report.write_text(json.dumps(record, ensure_ascii=False, indent=2), encoding="utf-8")
    save()
    print("사전 점검 통과. NAT 중지 요청:", old_id, "| 기록:", report, flush=True)
    ec2.stop_instances(InstanceIds=[old_id])
    deadline = time.monotonic() + 25 * 60
    while time.monotonic() < deadline:
        for member in group()["Instances"]:
            if member["InstanceId"] == old_id or member["LifecycleState"] != "InService":
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
