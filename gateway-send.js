#!/usr/bin/env node
/**
 * gateway-send.js — OpenClawのcallGateway APIでエージェントにメッセージ送信
 * 
 * 2段階方式:
 *   1. chat.inject — メッセージ本文をassistantトランスクリプトとして書き込み（改行保持）
 *   2. chat.send  — 短い起動メッセージを送信してrunを開始（user入力として）
 * 
 * これにより:
 *   - 改行を含むメッセージが正しく保持される（chat.sendは改行を消す）
 *   - エージェントがuser入力として認識して応答する（chat.injectだけだとassistant発言扱い）
 * 
 * Usage: node gateway-send.js <sessionKey> <message>
 */

const [,, sessionKey, message] = process.argv;
if (!sessionKey || !message) {
  console.error('Usage: node gateway-send.js <sessionKey> <message>');
  process.exit(1);
}

async function main() {
  const { n: callGateway, s: ADMIN_SCOPE } = await import('/usr/lib/node_modules/openclaw/dist/call-BUD9fxqU.js');
  const { h: GATEWAY_CLIENT_NAMES, m: GATEWAY_CLIENT_MODES } = await import('/usr/lib/node_modules/openclaw/dist/message-channel-CeD-0oOz.js');
  const crypto = await import('crypto');
  
  const opts = {
    mode: GATEWAY_CLIENT_MODES.CLI,
    clientName: GATEWAY_CLIENT_NAMES.CLI,
    scopes: [ADMIN_SCOPE],
  };

  try {
    // Step 1: メッセージ本文をassistantトランスクリプトに書き込み（改行保持）
    const injectResult = await callGateway({
      ...opts,
      method: 'chat.inject',
      params: { sessionKey, message },
    });
    
    if (injectResult?.error) {
      console.error('chat.inject error:', JSON.stringify(injectResult.error));
      process.exit(1);
    }

    // Step 2: 短い起動メッセージを送信（user入力 → runを開始）
    const sendResult = await callGateway({
      ...opts,
      method: 'chat.send',
      params: {
        sessionKey,
        message: '上記の [devbar] メッセージに従って作業を開始しろ。',
        idempotencyKey: crypto.randomUUID(),
      },
    });
    
    if (sendResult?.error) {
      console.error('chat.send error:', JSON.stringify(sendResult.error));
      process.exit(1);
    }
    
    process.exit(0);
  } catch (err) {
    console.error('Error:', err.message);
    process.exit(1);
  }
}

main();
