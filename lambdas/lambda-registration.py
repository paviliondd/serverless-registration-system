import os
import json
import uuid
import time
import logging
from typing import Dict, Any, Optional
import boto3
from botocore.config import Config
from botocore.exceptions import ClientError

# ===== Env =====
TABLE_NAME = os.getenv("TABLE_NAME")  # e.g. d-dva-ddb-registration
QUEUE_URL = os.getenv("QUEUE_URL")    # e.g. https://sqs.<region>.amazonaws.com/<acct>/d-dva-sqs-email
CORS_ORIGIN = os.getenv("CORS_ORIGIN", "*")  # In production, set to specific domain
if not TABLE_NAME or not QUEUE_URL:
    raise RuntimeError("Missing env vars: TABLE_NAME and/or QUEUE_URL")

LOG_LEVEL = os.getenv("LOG_LEVEL", "INFO").upper()

# ===== SDK clients (reuse) =====
_cfg = Config(
    retries={"max_attempts": 5, "mode": "standard"},
    read_timeout=3,
    connect_timeout=1,
    user_agent_extra="dva-registration/1.0"
)
_sess = boto3.Session()
_ddb = _sess.resource("dynamodb", config=_cfg)
_table = _ddb.Table(TABLE_NAME)
_sqs = _sess.client("sqs", config=_cfg)

# ===== Logging =====
logger = logging.getLogger()
logger.setLevel(LOG_LEVEL)

def create_response(status_code: int, body: Dict[str, Any], cors_origin: str = CORS_ORIGIN) -> Dict[str, Any]:
    """Create standardized API response with CORS headers"""
    return {
        "statusCode": status_code,
        "headers": {
            "Access-Control-Allow-Origin": cors_origin,
            "Access-Control-Allow-Headers": "Content-Type",
            "Access-Control-Allow-Methods": "OPTIONS,POST",
            "Content-Type": "application/json"
        },
        "body": json.dumps(body)
    }

def validate_input(body: Dict[str, Any]) -> Optional[str]:
    """Validate input fields, return error message if invalid"""
    required_fields = ["first_name", "last_name", "email"]
    
    # Check required fields
    for field in required_fields:
        if not body.get(field):
            return f"Missing required field: {field}"
        
        # Validate string fields
        if not isinstance(body[field], str):
            return f"Invalid type for {field}: must be string"
        
        # Trim and validate length
        value = body[field].strip()
        if not value:
            return f"Empty value for {field} not allowed"
        
        if field in ["first_name", "last_name"] and len(value) > 100:
            return f"{field} too long: max 100 characters"
        
        if field == "email":
            if len(value) > 254:
                return "Email too long: max 254 characters"
            if "@" not in value or "." not in value:
                return "Invalid email format"
    
    return None

def _parse_body(event: Dict[str, Any]) -> Dict[str, Any]:
    """Parse and decode request body"""
    try:
        if isinstance(event.get("body"), str):
            return json.loads(event["body"] or "{}")
        return event.get("body", {})
    except json.JSONDecodeError:
        logger.error({"error": "invalid_json", "body": event.get("body")})
        return {}

def handle_options_request(event: Dict[str, Any]) -> Dict[str, Any]:
    """Handle OPTIONS request for CORS preflight"""
    return create_response(200, {"status": "ok"})

def lambda_handler(event: Dict[str, Any], context: Any) -> Dict[str, Any]:
    """Main Lambda handler"""
    # Handle CORS preflight
    if event.get("httpMethod") == "OPTIONS":
        return handle_options_request(event)

    # Parse body
    body = _parse_body(event)
    if not body:
        return create_response(400, {"error": "Invalid request body"})

    # Validate input
    error = validate_input(body)
    if error:
        return create_response(400, {"error": error})

    # Prepare item
    item = {
        "id": str(uuid.uuid4()),
        "first_name": body["first_name"].strip(),
        "last_name": body["last_name"].strip(),
        "email": body["email"].strip().lower(),
        "status": "QUEUED",
        "created_at": int(time.time()),
    }

    # 1) Write to DynamoDB
    try:
        _table.put_item(Item=item)
    except ClientError as e:
        error_code = e.response.get("Error", {}).get("Code", "UNKNOWN")
        logger.error({
            "step": "ddb_put_failed",
            "error_code": error_code,
            "message": str(e),
            "registration_id": item["id"]
        })
        return create_response(500, {"error": "Database operation failed"})
    except Exception as e:
        logger.error({
            "step": "ddb_put_exception",
            "error": str(e),
            "registration_id": item["id"]
        })
        return create_response(500, {"error": "Internal server error"})

    # 2) Send message to SQS
    try:
        _sqs.send_message(
            QueueUrl=QUEUE_URL,
            MessageBody=json.dumps(item, ensure_ascii=False)
        )
    except (ClientError, Exception) as e:
        logger.error({
            "step": "sqs_send_failed",
            "error": str(e),
            "registration_id": item["id"]
        })
        return create_response(500, {"error": "Message queue operation failed"})

    logger.info({
        "message": "registration_accepted",
        "registration_id": item["id"],
        "email": item["email"]
    })
    
    return create_response(200, {"id": item["id"]})