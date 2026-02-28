#!/usr/bin/env node
/**
 * gateway-send.js — Gateway chat.send経由でメッセージをcollectキューに積む
 * 
 * openclaw agent CLIはrun中のセッションをabortする。
 * chat.sendはcollectキューに積まれるため、run中でも安全。
 * ただしchat.sendは改行を消す。催促など短いメッセージ専用。
 * 
 * Usage: node gateway-send.js <sessionKey> <message>
 */

const [,, sessionKey, message] = process.argv;
if (!sessionKey || !message) {
  console.error('Usage: node gateway-send.js <sessionKey> <message>');
  process.exit(1);
}

async function main() {
  const fs = await import('fs');
  const callFile = fs.readdirSync('/usr/lib/node_modules/openclaw/dist/')
    .filter(f => f.startsWith('call-') && f.endsWith('.js')).sort().pop();
  const { n: callGateway, s: ADMIN_SCOPE } = await import(`/usr/lib/node_modules/openclaw/dist/${callFile}`);
  const distDir = '/usr/lib/node_modules/openclaw/dist/';
  const mcCandidates = fs.readdirSync(distDir)
    .filter(f => f.startsWith('message-channel-') && f.endsWith('.js'));
  // Find the file that exports GATEWAY_CLIENT_NAMES as 'h' (export signature varies between builds)
  let mcFile;
  for (const c of mcCandidates) {
    const src = fs.readFileSync(distDir + c, 'utf8');
    if (src.includes('GATEWAY_CLIENT_NAMES as h')) { mcFile = c; break; }
  }
  if (!mcFile) throw new Error('No message-channel module found with expected exports');
  const { h: GATEWAY_CLIENT_NAMES, m: GATEWAY_CLIENT_MODES } = await import(`${distDir}${mcFile}`);
  const crypto = await import('crypto');
  
  try {
    const result = await callGateway({
      method: 'chat.send',
      params: {
        sessionKey,
        message,
        idempotencyKey: crypto.randomUUID(),
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
