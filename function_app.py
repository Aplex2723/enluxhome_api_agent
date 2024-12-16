import os
import logging
import requests
import azure.functions as func
from twilio.rest import Client

app = func.FunctionApp(http_auth_level=func.AuthLevel.FUNCTION)

@app.route(route="enluxhome", methods=["POST"])
def enluxhome(req: func.HttpRequest) -> func.HttpResponse:
    logging.info("Processing incoming Twilio WhatsApp message.")

    try:
        form_data = req.form
        from_number = form_data.get("From")   # e.g. "whatsapp:+1234567890"
        to_number = form_data.get("To")
        body = form_data.get("Body")
        wa_id = form_data.get("WaId")  # The sender's WhatsApp ID (without the +)
        if not wa_id:
            # Fallback: parse from 'From' if WaId not provided
            # Typically Twilio sends WaId, but let's ensure:
            if from_number and from_number.startswith("whatsapp:+"):
                wa_id = from_number.replace("whatsapp:+", "")
        
        logging.info(f"Incoming message from +{wa_id}: {body}")

        # 1. Check if the user exists in Airtable
        airtable_api_key = os.environ["AIRTABLE_API_KEY"]
        airtable_base_id = os.environ["AIRTABLE_BASE_ID"]
        airtable_table_name = os.environ["AIRTABLE_TABLE_NAME"]

        headers = {
            "Authorization": f"Bearer {airtable_api_key}",
            "Content-Type": "application/json"
        }

        # Filter by WhatsApp field (exact match)
        # If your WhatsApp column is named 'WhatsApp', adjust if needed
        filter_formula = f"{{WhatsApp}} = '+{wa_id}'"
        airtable_url = f"https://api.airtable.com/v0/{airtable_base_id}/{airtable_table_name}"
        params = {
            "filterByFormula": filter_formula
        }

        response = requests.get(airtable_url, headers=headers, params=params)
        response.raise_for_status()
        records = response.json().get("records", [])

        user_found = len(records) > 0
        flowise_session_id = None
        if user_found:
            # Extract the FlowiseSessionID from the first matching record
            flowise_session_id = records[0]["fields"].get("FlowiseSessionID", None)
        
        # 2. Make the API call to Flowise Agent
        flowise_url = os.environ.get("FLOWISE_AGENT_URL")
        flowise_token = os.environ.get("FLOWISE_AGENT_TOKEN")
        if not flowise_url or not flowise_token:
            logging.error("Flowise environment variables not set properly.")
            return func.HttpResponse("Configuration error.", status_code=500)

        headers = {
            "Authorization": f"Bearer {flowise_token}",
            "Content-Type": "application/json"
        }

        # Construct the payload
        override_config = {
            "vars": {
                "number": f"+{wa_id}"
            }
        }
        if user_found and flowise_session_id:
            override_config["sessionId"] = flowise_session_id

        payload = {
            "question": body,
            "overrideConfig": override_config
        }

        agent_response = requests.post(flowise_url, headers=headers, json=payload)
        agent_response.raise_for_status()
        agent_data = agent_response.json()

        # Extract the agent's response text
        agent_text = agent_data.get("data", {}).get("text", "")
        new_session_id = agent_data.get("data", {}).get("sessionId", None)

        # 3. If user not found, create a new Airtable record with the new sessionId
        if not user_found and new_session_id:
            new_record_payload = {
                "fields": {
                    "WhatsApp": f"+{wa_id}",
                    "FlowiseSessionID": new_session_id
                }
            }

            create_resp = requests.post(airtable_url, headers=headers, json=new_record_payload)
            create_resp.raise_for_status()
            logging.info("New Airtable record created.")

        # 4. Respond to user via Twilio
        account_sid = os.environ["TWILIO_ACCOUNT_SID"]
        auth_token = os.environ["TWILIO_AUTH_TOKEN"]
        client = Client(account_sid, auth_token)
        twilio_from = os.environ["TWILIO_FROM_NUMBER"]

        message = client.messages.create(
            body=agent_text,
            from_=twilio_from,
            to=from_number
        )

        logging.info(f"Confirmation message sent with SID: {message.sid}")

        return func.HttpResponse(status_code=200)

    except Exception as e:
        logging.exception("Error processing request")
        return func.HttpResponse("An error occurred.", status_code=500)