#!/usr/bin/env node
/**
 * gateway-send.js — OpenClawのcallGateway APIを直接使ってchat.sendを実行
 * 
 * openclaw agent CLIはrun中のセッションをabortする。
 * chat.sendはqueueに積まれるため、run中でも安全。
 * 
 * Usage: node gateway-send.js <sessionKey> <message>
 */

const [,, sessionKey, message] = process.argv;
if (!sessionKey || !message) {
  console.error('Usage: node gateway-send.js <sessionKey> <message>');
  process.exit(1);
}

async function main() {
  // OpenClawの内部API を使う
  const { n: callGateway, s: ADMIN_SCOPE } = await import('/usr/lib/node_modules/openclaw/dist/call-BUD9fxqU.js');
  const { h: GATEWAY_CLIENT_NAMES, m: GATEWAY_CLIENT_MODES } = await import('/usr/lib/node_modules/openclaw/dist/message-channel-CeD-0oOz.js');
  const crypto = await import('crypto');
  
  try {
    const result = await callGateway({
      method: 'chat.inject',
      params: {
        sessionKey,
        message,
      },
      mode: GATEWAY_CLIENT_MODES.CLI,
      clientName: GATEWAY_CLIENT_NAMES.CLI,
      scopes: [ADMIN_SCOPE],
    });
    
    if (result?.error) {
      console.error('chat.send error:', JSON.stringify(result.error));
      process.exit(1);
    }
    process.exit(0);
  } catch (err) {
    console.error('Error:', err.message);
    process.exit(1);
  }
}

main();
