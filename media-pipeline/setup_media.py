#!/usr/bin/env python3
"""Create (or update) the AWS side of call recording and real-time transcription, in one go.

    pip install boto3
    aws configure                      # or: aws sso login --profile ...; any credentials boto3 finds
    python media-pipeline/setup_media.py --bridge-url https://<public bridge address> --allowed-ip <BroadWorks public IP>

What it creates (names start with --name, default "telqon-media"; region --region, default eu-west-2):
    S3 bucket            <name>-recordings-<account>   private, encrypted, recordings expire after --keep-days
    Kinesis stream       <name>-insights               transcript lines and recording notices (on-demand)
    Secrets Manager      <name>/media-ingest-token     the bridge's media pipeline token
    IAM roles            <name>-pipeline, <name>-forwarder
    Lambda               <name>-forwarder              posts lines and recordings to the bridge (media-pipeline/forwarder)
    Call analytics       <name>-config                 Transcribe Call Analytics (--language) + S3 recording + Kinesis
    Voice Connector      <name>                        encryption off, accepts SIP from --allowed-ip, streaming on

It prints the Voice Connector address for the BroadWorks recording platform and the values for the
bridge's Settings page. Run it again to update (new bridge address, IPs, forwarder code). Nothing is
deleted unless you pass --delete (the bucket and its recordings only with --delete-bucket too).
"""
import argparse
import io
import json
import secrets
import sys
import time
import zipfile
from pathlib import Path

try:
    import boto3
    from botocore.exceptions import ClientError
except ImportError:
    sys.exit("pip install boto3 first")

HERE = Path(__file__).resolve().parent


def step(msg):
    print(f"• {msg}", flush=True)


def retry(fn, what, tries=10, wait=3, codes=("InvalidParameterValueException", "AccessDeniedException",
                                             "BadRequestException", "ForbiddenException")):
    """New IAM roles take a few seconds before other services accept them."""
    for i in range(tries):
        try:
            return fn()
        except ClientError as e:
            code = e.response["Error"]["Code"]
            if code in codes and i < tries - 1:
                time.sleep(wait)
                continue
            raise SystemExit(f"{what} failed: {code}: {e.response['Error'].get('Message')}")


class Setup:
    def __init__(self, a):
        self.a = a
        self.s = boto3.session.Session(region_name=a.region)
        self.account = self.s.client("sts").get_caller_identity()["Account"]
        self.name = a.name
        self.bucket = a.bucket or f"{a.name}-recordings-{self.account}"
        self.stream = f"{a.name}-insights"
        self.secret = f"{a.name}/media-ingest-token"
        self.fn = f"{a.name}-forwarder"
        self.config = f"{a.name}-config"
        self.c = {k: self.s.client(k) for k in ("s3", "kinesis", "secretsmanager", "iam", "lambda",
                                                  "chime-sdk-voice", "chime-sdk-media-pipelines")}

    # ------------------------------------------------------------------ checks
    def check_services(self):
        step(f"account {self.account}, region {self.a.region}")
        try:
            self.c["chime-sdk-voice"].list_voice_connectors(MaxResults=1)
            self.c["chime-sdk-media-pipelines"].list_media_insights_pipeline_configurations(MaxResults=1)
        except ClientError as e:
            msg = e.response["Error"].get("Message") or ""
            if "service control policy" in msg:
                hint = ("An organization policy (SCP) blocks Chime SDK for this account, in every region. New-style AWS "
                        "accounts get this until advanced features are activated: AWS console > account menu > AWS "
                        "Settings (or Billing > Account plan) > activate advanced AWS features. If that isn't offered, "
                        "ask AWS Support (account and billing cases are free) to allow Amazon Chime SDK, or use an "
                        "account created with the advanced sign-up. If the organization is your company's, its admin "
                        "must allow chime:* in the SCP.")
            elif "not enabled" in msg.lower():
                hint = ("AWS hasn't enabled Chime SDK voice for this account yet. Open the Chime SDK console in this "
                        "region (Voice connectors) once; new or just-upgraded accounts can take up to a day; otherwise "
                        "open a Support case (Account and billing, free) asking to enable Amazon Chime SDK Voice "
                        "Connector and media pipelines for the account.")
            elif "AccessDenied" in e.response["Error"].get("Code", "") or "not authorized" in msg:
                hint = "This IAM user lacks Chime SDK permissions (attach AdministratorAccess for the setup)."
            else:
                hint = "Free plan accounts must upgrade; or try another region with --region, e.g. eu-central-1."
            raise SystemExit(f"Chime SDK isn't available to this account in {self.a.region}: {msg}\n\n{hint}")
        except Exception as e:
            raise SystemExit(f"Chime SDK not reachable in {self.a.region}: {e}")

    # -------------------------------------------------------------------- S3
    def bucket_setup(self):
        s3 = self.c["s3"]
        try:
            s3.head_bucket(Bucket=self.bucket)
            step(f"bucket {self.bucket} exists")
        except ClientError:
            kw = {} if self.a.region == "us-east-1" else {
                "CreateBucketConfiguration": {"LocationConstraint": self.a.region}}
            s3.create_bucket(Bucket=self.bucket, **kw)
            step(f"bucket {self.bucket} created")
        s3.put_public_access_block(Bucket=self.bucket, PublicAccessBlockConfiguration={
            "BlockPublicAcls": True, "IgnorePublicAcls": True, "BlockPublicPolicy": True, "RestrictPublicBuckets": True})
        s3.put_bucket_encryption(Bucket=self.bucket, ServerSideEncryptionConfiguration={
            "Rules": [{"ApplyServerSideEncryptionByDefault": {"SSEAlgorithm": "AES256"}}]})
        s3.put_bucket_lifecycle_configuration(Bucket=self.bucket, LifecycleConfiguration={"Rules": [{
            "ID": "expire-recordings", "Status": "Enabled", "Filter": {"Prefix": ""},
            "Expiration": {"Days": self.a.keep_days}}]})
        # the media pipeline service writes recordings into the bucket
        s3.put_bucket_policy(Bucket=self.bucket, Policy=json.dumps({"Version": "2012-10-17", "Statement": [{
            "Sid": "ChimeMediaPipelinesWrite", "Effect": "Allow",
            "Principal": {"Service": "mediapipelines.chime.amazonaws.com"},
            "Action": ["s3:PutObject", "s3:PutObjectAcl", "s3:PutObjectTagging"],
            "Resource": f"arn:aws:s3:::{self.bucket}/*",
            "Condition": {"StringEquals": {"aws:SourceAccount": self.account}}}]}))

    # --------------------------------------------------------------- Kinesis
    def stream_setup(self):
        k = self.c["kinesis"]
        try:
            arn = k.describe_stream_summary(StreamName=self.stream)["StreamDescriptionSummary"]["StreamARN"]
            step(f"stream {self.stream} exists")
        except ClientError:
            k.create_stream(StreamName=self.stream, StreamModeDetails={"StreamMode": "ON_DEMAND"})
            step(f"stream {self.stream} created, waiting for it")
            k.get_waiter("stream_exists").wait(StreamName=self.stream)
            arn = k.describe_stream_summary(StreamName=self.stream)["StreamDescriptionSummary"]["StreamARN"]
        self.stream_arn = arn

    # --------------------------------------------------------------- secret
    def secret_setup(self):
        sm = self.c["secretsmanager"]
        self.new_token = None
        try:
            self.secret_arn = sm.describe_secret(SecretId=self.secret)["ARN"]
            if self.a.token:
                sm.put_secret_value(SecretId=self.secret, SecretString=self.a.token)
                self.new_token = self.a.token
                step("pipeline token updated")
            else:
                step("pipeline token kept (in Secrets Manager)")
        except ClientError:
            tok = self.a.token or secrets.token_hex(32)
            self.secret_arn = sm.create_secret(Name=self.secret, SecretString=tok,
                                               Description="Bridge media pipeline token (Bearer)")["ARN"]
            self.new_token = tok
            step("pipeline token created")

    # ------------------------------------------------------------------ IAM
    def role(self, name, service, policy):
        iam = self.c["iam"]
        trust = {"Version": "2012-10-17", "Statement": [{"Effect": "Allow", "Principal": {"Service": service},
                                                          "Action": "sts:AssumeRole"}]}
        if service.startswith("mediapipelines"):
            trust["Statement"][0]["Condition"] = {"StringEquals": {"aws:SourceAccount": self.account}}
        try:
            arn = iam.get_role(RoleName=name)["Role"]["Arn"]
            iam.update_assume_role_policy(RoleName=name, PolicyDocument=json.dumps(trust))
        except ClientError:
            arn = iam.create_role(RoleName=name, AssumeRolePolicyDocument=json.dumps(trust))["Role"]["Arn"]
            step(f"role {name} created")
        iam.put_role_policy(RoleName=name, PolicyName="access", PolicyDocument=json.dumps(policy))
        return arn

    def roles_setup(self):
        r, acct = self.a.region, self.account
        self.pipeline_role = self.role(f"{self.name}-pipeline", "mediapipelines.chime.amazonaws.com", {
            "Version": "2012-10-17", "Statement": [
                {"Effect": "Allow", "Action": ["transcribe:StartCallAnalyticsStreamTranscription",
                                               "transcribe:StartStreamTranscription"], "Resource": "*"},
                {"Effect": "Allow", "Action": ["kinesisvideo:GetDataEndpoint", "kinesisvideo:GetMedia",
                                               "kinesisvideo:DescribeStream", "kinesisvideo:ListFragments",
                                               "kinesisvideo:GetMediaForFragmentList"],
                 "Resource": f"arn:aws:kinesisvideo:{r}:{acct}:stream/*"},
                {"Effect": "Allow", "Action": ["kinesis:PutRecord", "kinesis:PutRecords"], "Resource": self.stream_arn},
                {"Effect": "Allow", "Action": ["s3:PutObject", "s3:PutObjectAcl", "s3:PutObjectTagging"],
                 "Resource": f"arn:aws:s3:::{self.bucket}/*"}]})
        self.forwarder_role = self.role(f"{self.name}-forwarder", "lambda.amazonaws.com", {
            "Version": "2012-10-17", "Statement": [
                {"Effect": "Allow", "Action": ["logs:CreateLogGroup", "logs:CreateLogStream", "logs:PutLogEvents"],
                 "Resource": f"arn:aws:logs:{r}:{acct}:*"},
                {"Effect": "Allow", "Action": ["kinesis:GetRecords", "kinesis:GetShardIterator", "kinesis:DescribeStream",
                                               "kinesis:DescribeStreamSummary", "kinesis:ListShards",
                                               "kinesis:ListStreams", "kinesis:SubscribeToShard"],
                 "Resource": self.stream_arn},
                {"Effect": "Allow", "Action": "secretsmanager:GetSecretValue", "Resource": self.secret_arn}]})

    # ---------------------------------------------------------------- Lambda
    def lambda_setup(self):
        lam = self.c["lambda"]
        buf = io.BytesIO()
        with zipfile.ZipFile(buf, "w", zipfile.ZIP_DEFLATED) as z:
            z.write(HERE / "forwarder" / "handler.py", "handler.py")
        code = buf.getvalue()
        env = {"Variables": {"BRIDGE_URL": self.a.bridge_url.rstrip("/"), "SECRET_ARN": self.secret_arn,
                             "SEND_PARTIALS": "1", "LOG_RAW": "20"}}
        try:
            lam.get_function(FunctionName=self.fn)
            lam.update_function_code(FunctionName=self.fn, ZipFile=code)
            lam.get_waiter("function_updated_v2").wait(FunctionName=self.fn)
            lam.update_function_configuration(FunctionName=self.fn, Environment=env, Role=self.forwarder_role)
            step(f"function {self.fn} updated")
        except ClientError as e:
            if e.response["Error"]["Code"] != "ResourceNotFoundException":
                raise
            retry(lambda: lam.create_function(FunctionName=self.fn, Runtime="python3.12", Handler="handler.lambda_handler",
                                              Role=self.forwarder_role, Code={"ZipFile": code}, Timeout=30,
                                              MemorySize=256, Environment=env,
                                              Description="Chime call analytics -> bridge media API"),
                  "creating the forwarder function")
            lam.get_waiter("function_active_v2").wait(FunctionName=self.fn)
            step(f"function {self.fn} created")
        maps = lam.list_event_source_mappings(FunctionName=self.fn, EventSourceArn=self.stream_arn)["EventSourceMappings"]
        if not maps:
            retry(lambda: lam.create_event_source_mapping(
                FunctionName=self.fn, EventSourceArn=self.stream_arn, StartingPosition="LATEST", BatchSize=50,
                MaximumBatchingWindowInSeconds=1, BisectBatchOnFunctionError=True, MaximumRetryAttempts=5,
                FunctionResponseTypes=["ReportBatchItemFailures"]), "connecting the stream to the function")
            step("stream connected to the function")

    # ------------------------------------------------- call analytics config
    def config_setup(self):
        mp = self.c["chime-sdk-media-pipelines"]
        elements = [
            {"Type": "AmazonTranscribeCallAnalyticsProcessor",
             "AmazonTranscribeCallAnalyticsProcessorConfiguration": {
                 "LanguageCode": self.a.language, "EnablePartialResultsStabilization": True,
                 "PartialResultsStability": "high"}},
            {"Type": "KinesisDataStreamSink", "KinesisDataStreamSinkConfiguration": {"InsightsTarget": self.stream_arn}},
            {"Type": "S3RecordingSink", "S3RecordingSinkConfiguration": {
                "Destination": f"arn:aws:s3:::{self.bucket}", "RecordingFileFormat": "Wav"}},
        ]
        existing = next((c for c in mp.list_media_insights_pipeline_configurations(MaxResults=100)
                         ["MediaInsightsPipelineConfigurations"]
                         if c["MediaInsightsPipelineConfigurationName"] == self.config), None)
        if existing:
            arn = existing["MediaInsightsPipelineConfigurationArn"]
            retry(lambda: mp.update_media_insights_pipeline_configuration(
                Identifier=arn, ResourceAccessRoleArn=self.pipeline_role, Elements=elements),
                "updating the call analytics configuration")
            step(f"call analytics configuration {self.config} updated")
        else:
            arn = retry(lambda: mp.create_media_insights_pipeline_configuration(
                MediaInsightsPipelineConfigurationName=self.config, ResourceAccessRoleArn=self.pipeline_role,
                Elements=elements)["MediaInsightsPipelineConfiguration"]["MediaInsightsPipelineConfigurationArn"],
                "creating the call analytics configuration")
            step(f"call analytics configuration {self.config} created ({self.a.language})")
        self.config_arn = arn

    # -------------------------------------------------------- Voice Connector
    def voice_connector_setup(self):
        vc = self.c["chime-sdk-voice"]
        found = next((v for v in vc.list_voice_connectors(MaxResults=99)["VoiceConnectors"]
                      if v["Name"] == self.name), None)
        if found:
            vid = found["VoiceConnectorId"]
            step(f"Voice Connector {self.name} exists")
        else:
            found = vc.create_voice_connector(Name=self.name, RequireEncryption=False,
                                              AwsRegion=self.a.region)["VoiceConnector"]
            vid = found["VoiceConnectorId"]
            step(f"Voice Connector {self.name} created (encryption off)")
        cidrs = [ip if "/" in ip else f"{ip}/32" for ip in self.a.allowed_ip]
        vc.put_voice_connector_termination(VoiceConnectorId=vid, Termination={
            "CpsLimit": 1, "CallingRegions": ["GB"], "CidrAllowedList": cidrs, "Disabled": False})
        retry(lambda: vc.put_voice_connector_streaming_configuration(VoiceConnectorId=vid, StreamingConfiguration={
            "DataRetentionInHours": 1, "Disabled": False,
            "MediaInsightsConfiguration": {"Disabled": False, "ConfigurationArn": self.config_arn}}),
            "turning on streaming with call analytics")
        step(f"accepting SIP from {', '.join(cidrs)}; streaming to call analytics on")
        self.vc_host = vc.get_voice_connector(VoiceConnectorId=vid)["VoiceConnector"]["OutboundHostName"]

    # ----------------------------------------------------------------- delete
    def delete(self):
        vc, mp, lam = self.c["chime-sdk-voice"], self.c["chime-sdk-media-pipelines"], self.c["lambda"]
        for v in vc.list_voice_connectors(MaxResults=99)["VoiceConnectors"]:
            if v["Name"] == self.name:
                vid = v["VoiceConnectorId"]
                for f in (vc.delete_voice_connector_streaming_configuration, vc.delete_voice_connector_termination):
                    try:
                        f(VoiceConnectorId=vid)
                    except ClientError:
                        pass
                vc.delete_voice_connector(VoiceConnectorId=vid)
                step("Voice Connector deleted")
        for c in mp.list_media_insights_pipeline_configurations(MaxResults=100)["MediaInsightsPipelineConfigurations"]:
            if c["MediaInsightsPipelineConfigurationName"] == self.config:
                mp.delete_media_insights_pipeline_configuration(Identifier=c["MediaInsightsPipelineConfigurationArn"])
                step("call analytics configuration deleted")
        try:
            lam.delete_function(FunctionName=self.fn)
            step("function deleted")
        except ClientError:
            pass
        try:
            self.c["kinesis"].delete_stream(StreamName=self.stream, EnforceConsumerDeletion=True)
            step("stream deleted")
        except ClientError:
            pass
        try:
            self.c["secretsmanager"].delete_secret(SecretId=self.secret, ForceDeleteWithoutRecovery=True)
            step("token secret deleted")
        except ClientError:
            pass
        iam = self.c["iam"]
        for r in (f"{self.name}-pipeline", f"{self.name}-forwarder"):
            try:
                iam.delete_role_policy(RoleName=r, PolicyName="access")
                iam.delete_role(RoleName=r)
                step(f"role {r} deleted")
            except ClientError:
                pass
        if self.a.delete_bucket:
            b = boto3.resource("s3", region_name=self.a.region).Bucket(self.bucket)
            try:
                b.objects.all().delete()
                b.delete()
                step(f"bucket {self.bucket} and its recordings deleted")
            except ClientError as e:
                print(f"  bucket not deleted: {e}")
        else:
            print(f"  bucket {self.bucket} kept (add --delete-bucket to remove it and its recordings)")

    def run(self):
        self.check_services()
        self.bucket_setup()
        self.stream_setup()
        self.secret_setup()
        self.roles_setup()
        self.lambda_setup()
        self.config_setup()
        self.voice_connector_setup()
        print(f"""
Done. Next:

1. BroadWorks (system admin): Call Recording Platform
     address    {self.vc_host}   port 5060, UDP or TCP, no TLS
     media      Dual stream (agent and customer on separate channels)
   assign it to the enterprise/group, give the agents the Call Recording service ("Always" for tests).
   Firewall: allow SIP 5060 and RTP to AWS's Chime SDK Voice Connector IP ranges for {self.a.region}.

2. Bridge Settings page > Recording and transcripts
     Pipeline token        {self.new_token or '(unchanged; read it with: aws secretsmanager get-secret-value --secret-id ' + self.secret + ')'}
     Recording storage     s3
     S3 bucket             {self.bucket}
     S3 region             {self.a.region}
   The bridge signs playback links with your AWS credentials (pip install boto3 on the bridge machine).

3. Watch the forwarder during the first call:
     aws logs tail /aws/lambda/{self.fn} --follow --region {self.a.region}
""")


def main():
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--bridge-url", help="public HTTPS address of the bridge (e.g. a Cloudflare Tunnel address)")
    ap.add_argument("--allowed-ip", action="append", default=[],
                    help="public IP (or CIDR) the BroadWorks recording SIP comes from; repeat for several")
    ap.add_argument("--region", default="eu-west-2")
    ap.add_argument("--name", default="telqon-media")
    ap.add_argument("--bucket", help="bucket name (default <name>-recordings-<account>)")
    ap.add_argument("--language", default="en-GB", help="Transcribe Call Analytics language (default en-GB)")
    ap.add_argument("--keep-days", type=int, default=30, help="delete recordings after this many days (default 30)")
    ap.add_argument("--token", help="use this pipeline token instead of a generated one (32+ characters)")
    ap.add_argument("--delete", action="store_true", help="remove everything this script created")
    ap.add_argument("--delete-bucket", action="store_true", help="with --delete: also the bucket and recordings")
    a = ap.parse_args()
    if a.token and len(a.token) < 32:
        sys.exit("--token must be at least 32 characters")
    s = Setup(a)
    if a.delete:
        return s.delete()
    if not a.bridge_url or not a.bridge_url.startswith("https://"):
        sys.exit("--bridge-url https://... is required (AWS must reach the bridge over HTTPS)")
    if not a.allowed_ip:
        sys.exit("--allowed-ip is required: the public IP BroadWorks' recording SIP comes from")
    s.run()


if __name__ == "__main__":
    main()
