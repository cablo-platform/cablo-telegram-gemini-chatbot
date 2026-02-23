import RAG
import os
import time
from collections import defaultdict
from cablo.services.telegram.actions import _get_client, telegram_login_request, telegram_login_confirm
from cablo.services.gemini.actions import generate_G2_flash


SESSION_FILE = "session.txt"
PROCESSED_FILE = "processed_ids.txt"

# In-memory storage for chat context
conversation_history = defaultdict(list)
MAX_HISTORY = 10

def load_processed_ids():

    """Reads previously handled message IDs from local file to avoid duplicate replies."""

    try:
        if os.path.exists(PROCESSED_FILE):
            with open(PROCESSED_FILE, 'r') as f:
                return set(int(line.strip()) for line in f if line.strip())
    except:
        pass
    return set()

def save_processed_id(msg_id):

    """Appends a processed message ID to the tracking file."""

    try:
        with open(PROCESSED_FILE, 'a') as f:
            f.write(f"{msg_id}\n")
    except:
        pass

def build_prompt_with_history(user_id, current_message):

    """
    Constructs a full prompt including RAG knowledge, chat history, and current input.
    """

    history = conversation_history[user_id]
    
    history_text = ""
    if history:
        history_text = "Previous conversation:\n"
        for i, (role, msg) in enumerate(history[-MAX_HISTORY:], 1):
            history_text += f"{role}: {msg}\n"
        history_text += "\n"

    # Merging RAG data, history, and the new message into one prompt
    prompt = f"""{RAG.KNOWLEDGE}

    {history_text}Current message: {current_message}

    Please respond appropriately based on the context and previous conversation."""
    
    return prompt

def update_history(user_id, role, message):
    """Adds a new message to the history and prunes old entries to keep it within limits."""
    conversation_history[user_id].append((role, message))

    # Keep history size manageable (2 * MAX_HISTORY for both User and Assistant)
    if len(conversation_history[user_id]) > MAX_HISTORY * 2:  
        conversation_history[user_id] = conversation_history[user_id][-MAX_HISTORY * 2:]

def main():

    """Main execution loop for the Telegram bot."""

    # Session handling logic
    try:
        if os.path.exists(SESSION_FILE):
            with open(SESSION_FILE, "r") as f:
                session_string = f.read().strip()
        else:
            session_string = ""
    except FileNotFoundError:
        session_string = ""
    client = _get_client(session_string)

    try:

        # Authentication block
        if not client.is_user_authorized():
            print("⚠️ Not authorized. Starting login flow...")
            
            phone = input("Please enter your phone: ")
            status, res = telegram_login_request({'phone': phone})
            print(status, res)
            if status == 200:
                code = input("Enter the code you received: ")
                confirm_body = {
                    'phone': phone,
                    'code': code,
                    'phone_code_hash': res['phone_code_hash'],
                    'temp_session': res['temp_session']
                }
                c_status, c_res = telegram_login_confirm(confirm_body)
                
                if c_status == 200:
                    print(f"✅ Login successful as {c_res['username']}")
                    new_token = c_res['access_token']
                    with open(SESSION_FILE, "w") as f:
                        f.write(new_token)
                    
                    client.disconnect()
                    return main() # Restart with authorized session
                else:
                    print(f"❌ Login failed: {c_res}")
                    return
        else:
            print("✅ Already logged in. Skipping login flow...")

        print("🚀 Gemini RAG Bot is starting...")

        processed_ids = load_processed_ids()
        print(f"✅ Loaded {len(processed_ids)} processed message IDs")
        
        try:
            last_messages = client.get_messages(None, limit=1)
            if last_messages:
                last_id = last_messages[0].id
                print(f"✅ Last message ID: {last_id}")
        except:
            pass
        
        last_check_time = time.time()
        
        while True:

            # Fetch recent messages (Manual Polling)
            try:
                messages = client.get_messages(None, limit=20)
                
                if messages:

                    # Process from oldest to newest
                    for msg in reversed(messages):

                        # Skip if: sent by bot, has no text, or already handled
                        if msg.out or not msg.text or msg.id in processed_ids:
                            continue
                        
                        if msg.is_private:
                            print(f"📩 Processing new message ID {msg.id}: {msg.text[:50]}")

                            # Prepare data for Gemini
                            update_history(msg.sender_id, "User", msg.text)
                            prompt_with_history = build_prompt_with_history(msg.sender_id, msg.text)
                            rpc_request = {
                                "jsonrpc": "2.0",
                                "method": "generate_content",
                                "params": {
                                    "knowledge": RAG.KNOWLEDGE,
                                    "prompt": prompt_with_history
                                },
                                "id": msg.sender_id
                            }
                            
                            print(f"📤 Sending RPC request to Gemini...")
                            
                            try:
                                response = generate_G2_flash(rpc_request)
                                
                                if isinstance(response, dict):
                                    if 'result' in response:
                                        result = response['result']
                                        # Dynamic response parsing
                                        if isinstance(result, dict) and 'content' in result:
                                            reply_text = result['content']
                                        else:
                                            reply_text = str(result)
                                        
                                        msg.reply(reply_text)
                                        print(f"✅ Replied to message {msg.id}")
                                        
                                        processed_ids.add(msg.id)
                                        save_processed_id(msg.id)
                                        
                                    elif 'error' in response:
                                        error_msg = response['error'].get('message', 'Unknown error')
                                        print(f"⚠️ Gemini Error: {error_msg}")
                                        
                                        if 'method' not in error_msg.lower():
                                            processed_ids.add(msg.id)
                                            save_processed_id(msg.id)
                                else:
                                    print(f"⚠️ Unexpected response type: {type(response)}")
                                    
                            except Exception as ex:
                                print(f"❌ Gemini Crash: {ex}")
                        
                        processed_ids.add(msg.id)
                        save_processed_id(msg.id)
                
                time.sleep(2)

                # Periodic memory management for processed_ids set
                if time.time() - last_check_time > 600:
                    print("🔄 Periodic cleanup...")
                    if len(processed_ids) > 1000:
                        processed_ids = set(list(processed_ids)[-1000:])
                    last_check_time = time.time()

            except Exception as e:
                print(f"⚠️ Loop Error: {e}")
                time.sleep(5)
    finally:
        client.disconnect()

if __name__ == "__main__":
    main()
