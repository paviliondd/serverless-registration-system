import os, json, logging
import boto3
from botocore.config import Config
from botocore.exceptions import ClientError

# ==== Env ====
TABLE_NAME = os.getenv("TABLE_NAME")
TOPIC_ARN  = os.getenv("TOPIC_ARN")
SES_SENDER = os.getenv("SES_SENDER")
LOG_LEVEL  = os.getenv("LOG_LEVEL", "INFO").upper()
if not (TABLE_NAME and TOPIC_ARN and SES_SENDER):
    raise RuntimeError("Missing env vars: TABLE_NAME / TOPIC_ARN / SES_SENDER")

# ==== SDK clients (reuse) ====
_cfg   = Config(retries={"max_attempts": 5, "mode": "standard"},
                read_timeout=5, connect_timeout=2,
                user_agent_extra="dva-sender/1.0")
_sess  = boto3.Session()
_ddb   = _sess.resource("dynamodb", config=_cfg)
_table = _ddb.Table(TABLE_NAME)
_sns   = _sess.client("sns", config=_cfg)
_ses   = _sess.client("ses", config=_cfg)

# ==== Logging ====
logger = logging.getLogger()
logger.setLevel(LOG_LEVEL)

def lambda_handler(event, context):
    failures = []  # for SQS partial batch response

    for rec in event.get("Records", []):
        msg_id = rec.get("messageId")
        try:
            payload = json.loads(rec["body"])
            rid   = payload.get("id")
            email = payload.get("email")
            first = payload.get("first_name", "")
            last  = payload.get("last_name", "")

            # 1) Send email to user via SES
            _ses.send_email(
                Source=SES_SENDER,
                Destination={"ToAddresses": [email]},
                Message={
                    "Subject": {"Data": "Registration confirmed"},
                    "Body":    {"Text": {"Data": f"Hi {first} {last},\nThanks for registering!"}}
                }
            )

            # 2) Notify admin via SNS
            _sns.publish(
                TopicArn=TOPIC_ARN,
                Subject="New registration",
                Message=json.dumps(
                    {"id": rid, "email": email, "first_name": first, "last_name": last},
                    ensure_ascii=False
                )
            )

            # 3) Update DynamoDB status -> SENT
            if rid:
                _table.update_item(
                    Key={"id": rid},
                    UpdateExpression="SET #s = :v",
                    ExpressionAttributeNames={"#s": "status"},
                    ExpressionAttributeValues={":v": "SENT"}
                )

            logger.info({"msg": "sender_processed", "id": rid, "messageId": msg_id})

        except ClientError as e:
            # Mark this record as failed -> only this one will be retried (partial batch)
            logger.error({
                "msg": "aws_client_error",
                "messageId": msg_id,
                "code": e.response.get("Error", {}).get("Code"),
                "detail": e.response.get("Error", {}).get("Message")
            })
            failures.append({"itemIdentifier": msg_id})
        except Exception as e:
            logger.exception({"msg": "unhandled", "messageId": msg_id})
            failures.append({"itemIdentifier": msg_id})

    # Partial batch response for SQS (prevents reprocessing successful records)
    if failures:
        return {"batchItemFailures": failures}
    return {"statusCode": 200}