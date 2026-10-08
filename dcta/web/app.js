'use strict';
const $ = id => document.getElementById(id);
const token = document.querySelector('meta[name="dcta-token"]').content;
const workspaceId = document.querySelector('meta[name="dcta-workspace"]').content;
try {
  if(localStorage.getItem('dcta.public-workspace')!==workspaceId){
    for(const key of Object.keys(localStorage))if(/^dcta[-.]/.test(key) && !['dcta.theme','dcta.accent','dcta.readAloud'].includes(key))localStorage.removeItem(key);
    localStorage.setItem('dcta.public-workspace',workspaceId);
  }
} catch {}
let session = '', config, voice = null, capture = null, voiceEpoch = 0, pendingCapture = false;
let playbackUrl = '', pollTimer = null, turnsInFlight = 0;
let currentDraft=null, review=null, reviewEpoch=0, reviewing=false, signing=false, demoKey=null, pendingPayment=null;
let invalidating=Promise.resolve(), currentPayees=[];
let sessionEpoch=0, refreshIssued=0, lastStateSequence=-1, reviewGate={ready:true}, reviewTimer=null;
let sendingText=false, switchingSession=false;
let recentIssued=0, creatingSession=false, initializing=false, renderedRecentQuery='';
let menuSession='', menuAnchor=null, deleteTarget=null, deletingSession='';
let deletedIssued=0, deletedTimer=null;
const recentButtons=new Map();
const recentNames=new Map();
let renameTarget=null,renamingSession='',menuFocus=0;
const editorDrafts=new Map(), voiceCancellations=new Map();
// Appearance is applied before first render to avoid a white flash; stored per browser only.
// Public version label shown in Settings. The full build string stays in the README/evidence.
const PUBLIC_VERSION='v 0.10';
const THEMES=['light','grey','dark'];
const THEME_ID={light:'themeWhite',grey:'themeGrey',dark:'themeBlack'};
let theme='light';
function applyTheme(value){theme=THEMES.includes(value)?value:'light';try{document.documentElement.dataset.theme=theme;}catch{}return theme;}
function syncTheme(){const select=$('themeSelect');const current=THEMES.includes(theme)?theme:THEMES.includes(select&&select.value)?select.value:'light';
  for(const name of THEMES){const swatch=$(THEME_ID[name]);if(swatch)swatch.setAttribute('aria-pressed',String(name===current));}}
try{applyTheme(localStorage.getItem('dcta.theme'));}catch{applyTheme('light');}
// Accent colour from the Settings palette. Applied before first paint, like the theme.
const ACCENTS=['teal','blue','green','violet','amber','rose'];
let accent='teal';
const accentId=name=>'accent'+name.charAt(0).toUpperCase()+name.slice(1);
function applyAccent(value){accent=ACCENTS.includes(value)?value:'teal';try{document.documentElement.dataset.accent=accent;}catch{}return accent;}
function syncAccent(){for(const name of ACCENTS){const swatch=$(accentId(name));if(swatch)swatch.setAttribute('aria-pressed',String(name===accent));}}
try{applyAccent(localStorage.getItem('dcta.accent'));}catch{applyAccent('teal');}
const saved = (key, fallback='') => { try { return localStorage.getItem(key) || fallback; } catch { return fallback; } };
const save = (key, value) => { try { localStorage.setItem(key, value); } catch {} };
// Stable display identifiers. Keep symbolic host codes intact for control flow and audit lookup.
// Append new identifiers; never renumber existing ones. Meanings live in docs/ERROR_CODES.md.
const ISSUE_NUMBERS=Object.freeze({
  DEMO_CAPACITY:7001,DEMO_QUOTA:7002,VISITOR_EXPIRED:7003,DEMO_STORAGE:7004,
  NETWORK_ERROR:1001,REQUEST_TIMEOUT:1002,PROVIDER_CONFIG:1101,PROVIDER_AUTH:1102,
  PROVIDER_BALANCE:1103,PROVIDER_NETWORK:1104,PROVIDER_TIMEOUT:1105,PROVIDER_RATE_LIMIT:1106,
  PROVIDER_BUSY:1107,PROVIDER_HTTP_ERROR:1108,PROVIDER_INCOMPLETE:1109,PROVIDER_PROTOCOL:1110,
  PROVIDER_RESPONSE_SIZE:1111,PROVIDER_EMPTY_OR_OVERSIZE:1112,PROVIDER_UNEXPECTED_TOOL:1113,PROVIDER_UNAVAILABLE:1114,
  DISCREPANCY:2001,NEEDS_CLARIFICATION:2002,CHECK_ERROR:2003,STALE:2004,
  VALIDATOR_UNAVAILABLE:2005,VALIDATOR_CONTRACT:2006,REVIEW_NOT_READY:2007,CONTEXT_LIMIT:2008,
  MODEL_CONTRACT:2009,INVALID_JSON:2010,EVIDENCE:2011,TOOL_BUDGET:2012,TOOL_DENIED:2013,
  AMOUNT:3001,AMOUNT_RULE:3002,AMOUNT_VAGUE:3003,CURRENCY:3004,SELECTION:3005,
  SINGLE_LIMIT:3101,DAILY_LIMIT:3102,RATE_LIMIT:3103,INSUFFICIENT_FUNDS:3104,MIN_BALANCE:3105,
  KYC:3106,ACCOUNT:3107,PAYLOAD:3108,RECONCILIATION:3109,INTEGRITY:3110,
  VALIDATION_REQUIRED:4001,STALE_REVIEW:4002,STALE_AUTHORIZATION:4003,EXPIRED:4004,
  AUTHORIZATION_REQUIRED:4005,AUTH_STATE:4006,PROOF:4007,CLOCK_CHANGE:4008,KEY_LIMIT:4009,
  WEBAUTHN_ORIGIN:4010,DEVICE_CANCELLED:4011,DEVICE_UNAVAILABLE:4012,
  STEPUP_REQUIRED:4101,STEPUP_CODE:4102,STEPUP_EXPIRED:4103,STEPUP_STATE:4104,
  STEPUP_LIMIT:4105,STEPUP_KEY:4106,STEPUP_NOT_NEEDED:4107,SECURITY_PAUSE:4108,
  IDEMPOTENCY_CONFLICT:4201,EXECUTION_ERROR:4202,PENDING:4203,
  ASR_SETUP:5001,ASR_TIMEOUT:5002,ASR_FAILED:5003,AUDIO_FORMAT:5004,AUDIO_SIZE:5005,
  AUDIO_STORAGE:5006,VOICE_STALE:5007,VOICE_PENDING:5008,SPOKEN_AMOUNT:5009,
  MIC_DENIED:5010,MIC_UNAVAILABLE:5011,ASR_EMPTY:5012,
  ASR_AUTH:5013,ASR_SERVICE:5014,ASR_QUOTA:5015,ASR_NETWORK:5016,ASR_RATE_LIMIT:5017,ASR_RESPONSE:5018,
  HISTORY_REMOVED:6001,HISTORY_CHANGED:6002,HISTORY_BUSY:6003,HISTORY_CONFIRMATION:6004,
  HISTORY_EXPIRED:6005,TITLE:6006,TITLE_CONFLICT:6007,STALE_TURN:6008,TURN_COMPLETE:6009,LOCKED:6010,
  INPUT:9001,SCHEMA:9002,RESPONSE_INVALID:9003,FORBIDDEN:9004,NOT_FOUND:9005,
  BUSY:9006,CONFLICT:9007,STATE:9008,DISABLED:9009,SERVER:9010,
  ALREADY_RUNNING:9011,PORT_UNAVAILABLE:9012,UNKNOWN:9999
});
function issueCode(code){const number=Object.hasOwn(ISSUE_NUMBERS,code)?ISSUE_NUMBERS[code]:ISSUE_NUMBERS.UNKNOWN;return `DCTA-${number}`;}
// Reviewed user-facing explanations, independent of raw provider diagnostics or credentials.
// Keep the code for support and the original symbolic value for recovery decisions.
const ISSUE_HELP=Object.freeze({
  DEMO_CAPACITY:['The demo is busy','All visitor spaces are in use. Please try again later.'],
  DEMO_QUOTA:['The demo usage limit has been reached','You can still view your saved conversations and receipts. Please try new requests later.'],
  VISITOR_EXPIRED:['Your demo session has expired','Reload the page to start a new session with the preset accounts.'],
  DEMO_STORAGE:['This session has reached its recording limit','Continue with text to finish your request.'],
  NETWORK_ERROR:['Cannot reach the app','Check that the app is running and your connection is available. If you were sending a transfer, use Check original result before starting another payment.'],
  REQUEST_TIMEOUT:['The request took too long','The app did not respond in time. If you were sending a transfer, use Check original result to confirm its status before starting another payment.'],
  PROVIDER_CONFIG:['AI service is not configured','Check the model service settings and API key on the server, then try again.'],
  PROVIDER_AUTH:['AI service rejected its credentials','The server API key could not be verified. Check the key and its access permissions, then try again.'],
  PROVIDER_BALANCE:['AI service balance is insufficient','Check the model service balance or quota before trying again.'],
  PROVIDER_NETWORK:['Cannot connect to the AI service','Check the server connection to the model service, then try again.'],
  PROVIDER_TIMEOUT:['AI service timed out','A complete answer did not arrive in time. Wait a moment and try again.'],
  PROVIDER_RATE_LIMIT:['Too many AI requests','The model service has temporarily limited requests. Wait a moment before trying again.'],
  PROVIDER_BUSY:['AI service is busy','Another model request is still running. Wait for it to finish, then try again.'],
  PROVIDER_HTTP_ERROR:['AI service returned an error','The model service could not complete the request. Try again later; if it repeats, check the server logs using this reference.'],
  PROVIDER_INCOMPLETE:['AI response was incomplete','The answer ended before all required details arrived. Try again; if it repeats, check the model response limits.'],
  PROVIDER_PROTOCOL:['AI response could not be read','The model service returned an unexpected format. Try again; if it repeats, check the model service configuration.'],
  PROVIDER_RESPONSE_SIZE:['AI response was too large','The response exceeded the allowed size. Try a shorter request with fewer details.'],
  PROVIDER_EMPTY_OR_OVERSIZE:['AI answer was empty or too long','There was no usable answer within the allowed size. Try a shorter, more specific request.'],
  PROVIDER_UNEXPECTED_TOOL:['AI requested an unsupported action','The requested tool is not allowed. Restate your transfer using the amount, saved payee and source account.'],
  PROVIDER_UNAVAILABLE:['AI service is unavailable','The model service could not be used. Check its configuration and availability, then try again.'],
  DISCREPANCY:['The independent check found a mismatch','The checker could not match the draft to your original instructions. Check the amount, recipient and source account, clarify any differences in the conversation, then review again.'],
  NEEDS_CLARIFICATION:['Your instructions need clarification','The independent checker could not confirm every detail. State the exact amount, currency, recipient and source account in the conversation, then review again.'],
  CHECK_ERROR:['The independent check could not finish','No successful check was returned. Try the check again; if it keeps failing, use this reference to investigate.'],
  STALE:['The draft changed during the check','Open the latest draft, check its details and start a new review.'],
  VALIDATOR_UNAVAILABLE:['Independent checker is unavailable','The checking service is not ready. Check its configuration or try again later.'],
  VALIDATOR_CONTRACT:['The checker returned an unusable result','Its response did not meet the required format or evidence checks. Try again; if it repeats, check the validator configuration.'],
  REVIEW_NOT_READY:['The draft is not ready for review','Resolve the pending questions or confirmations shown in this conversation, then review again.'],
  CONTEXT_LIMIT:['There is too much context to check reliably','The current request exceeds the available context or lacks a usable parsed draft. Restate the complete transfer details clearly and review the resulting draft.'],
  MODEL_CONTRACT:['The AI could not produce a valid request','Its response did not meet the required structure. Restate the amount, recipient and source account, then try again.'],
  INVALID_JSON:['The AI response format was invalid','The structured answer could not be read. Try again with a clear, concise request.'],
  EVIDENCE:['A transaction detail could not be traced to your words','State the exact amount, currency, recipient and source account again so each field can be checked.'],
  TOOL_BUDGET:['The request needed too many steps','The tool limit was reached before the request finished. Try a simpler request.'],
  TOOL_DENIED:['This action is not allowed','The requested tool is unavailable for this operation. Use a supported transfer action.'],
  AMOUNT:['The amount is invalid','Enter a positive, exact SGD amount with at most two decimal places, such as SGD 50.00.'],
  AMOUNT_RULE:['The amount could not be calculated','The relative amount rule could not be resolved. Specify the exact SGD amount instead.'],
  AMOUNT_VAGUE:['The amount is not precise enough','State the exact amount you want to send, such as SGD 50.00.'],
  CURRENCY:['This currency is not supported','This prototype supports SGD. State your intended amount explicitly in SGD.'],
  SELECTION:['That choice is no longer available','Choose one of the options in the latest question, or restate the intended recipient and account.'],
  SINGLE_LIMIT:['The transfer exceeds the single-payment limit','Reduce the amount to fit the configured demo limit, then review the updated draft.'],
  DAILY_LIMIT:['The daily transfer limit would be exceeded','Check your completed transfers and the remaining demo allowance. Reduce the amount or wait for the limit to reset.'],
  RATE_LIMIT:['Transfers are being requested too quickly','Wait for the transaction frequency limit to reset before trying again.'],
  INSUFFICIENT_FUNDS:['The source account has insufficient funds','Check its available balance. Reduce the amount or choose another eligible source account.'],
  MIN_BALANCE:['This would breach your minimum balance','The transfer would leave less than the amount you asked to keep. Reduce the transfer or explicitly change the minimum-balance condition.'],
  KYC:['Identity verification is incomplete','This demo customer is not marked as verified. Check the demo identity status before reviewing a transfer.'],
  ACCOUNT:['The account cannot be used for this transfer','Check that the selected source and destination accounts are active and support SGD.'],
  PAYLOAD:['The transaction details are invalid','One or more fields or totals failed validation. Check the latest draft and restate any incorrect details.'],
  RECONCILIATION:['The ledger could not be reconciled','Check the saved transaction status and receipts. Use this reference to investigate before submitting another payment.'],
  INTEGRITY:['Saved data failed an integrity check','Use this reference to investigate the stored record before continuing with the transfer.'],
  VALIDATION_REQUIRED:['An independent check is required','Open the current draft and choose Review & approve before attempting authorization.'],
  STALE_REVIEW:['This review is out of date','The draft or its inputs have changed. Check the latest draft and review it again.'],
  STALE_AUTHORIZATION:['This approval is out of date','Open the latest draft and complete a new review and device approval.'],
  EXPIRED:['This approval has expired','Check the original result if the transfer was already submitted. Otherwise, review the current draft and approve it again.'],
  AUTHORIZATION_REQUIRED:['Device approval is required','Complete the review and approve the displayed draft on this device.'],
  AUTH_STATE:['The approval is no longer in the expected state','Check the saved transfer status, then reopen the current draft if a new review is needed.'],
  PROOF:['Device approval could not be verified','Check that you are using the registered device, then review and approve the current draft again.'],
  CLOCK_CHANGE:['The clock changed during approval','Check the device time, then start a new review so the approval has a valid expiry.'],
  KEY_LIMIT:['The device registration limit was reached','Check the registered device keys before adding another one.'],
  WEBAUTHN_ORIGIN:['Device verification is unavailable at this address','Open the app from its supported secure address or localhost, then try device verification again.'],
  DEVICE_CANCELLED:['Device verification was cancelled','When ready, reopen approval and complete the browser device-verification prompt.'],
  DEVICE_UNAVAILABLE:['Device verification is unavailable','Check that your browser and device support the verification method shown in the app.'],
  STEPUP_REQUIRED:['An additional security code is required','Use your security token to obtain the code and enter it in the approval dialog.'],
  STEPUP_CODE:['The security code is not valid','Enter the current six-digit code from your security token. Check it carefully before trying again.'],
  STEPUP_EXPIRED:['The security code has expired','Request a new code when available and enter it before it expires.'],
  STEPUP_STATE:['The security-code request is no longer active','Reopen the current approval and request a new code if required.'],
  STEPUP_LIMIT:['Too many codes have been requested','The code-request limit for this approval has been reached. Wait until another attempt is allowed.'],
  STEPUP_KEY:['The security token is unavailable','Check the configured demo security token before continuing with approval.'],
  STEPUP_NOT_NEEDED:['This transfer does not need an extra code','Return to the current approval and follow the steps shown there.'],
  SECURITY_PAUSE:['Approval is temporarily paused','Too many incorrect security codes were entered. Wait for the displayed countdown to finish before trying again.'],
  IDEMPOTENCY_CONFLICT:['The saved request has different transaction details','Check the original request result. Do not resubmit it with changed details.'],
  EXECUTION_ERROR:['The transfer result needs to be checked','An execution error occurred. Use Check original result to retrieve the saved status before starting another payment.'],
  PENDING:['An operation is still in progress','Wait for the current operation. For a submitted transfer, use Check original result to confirm its status.'],
  ASR_SETUP:['Speech recognition is not configured','Check the selected recognition provider and its setup, or type your request.'],
  ASR_AUTH:['Tencent speech recognition could not authenticate','Check the TRTC application ID, SDK signing key and application site on the server, or use text.'],
  ASR_SERVICE:['Tencent speech recognition is not enabled','Check this application’s speech recognition access in the TRTC console, or use text.'],
  ASR_QUOTA:['Tencent speech recognition has no available quota','Check the free allowance and billing status in TRTC Package Management, or use text.'],
  ASR_NETWORK:['Cannot reach Tencent speech recognition','Check the server’s connection and try again, or type your request.'],
  ASR_RATE_LIMIT:['Tencent speech recognition is busy','Wait a moment before recording again, or use text.'],
  ASR_RESPONSE:['Tencent returned an incomplete recognition result','Record again or type your request. No transcript was submitted.'],
  ASR_TIMEOUT:['Speech recognition timed out','Try a shorter recording or type your request instead.'],
  ASR_FAILED:['The recording could not be transcribed','Try recording again in a quiet place, or type your request.'],
  AUDIO_FORMAT:['This audio format is not supported','Upload a supported audio file or use the microphone to record a new message.'],
  AUDIO_SIZE:['The recording is outside the allowed size','Use a nonempty recording within the displayed duration and file-size limits.'],
  AUDIO_STORAGE:['Audio storage is full','Free space for recordings on the server, or type your request instead.'],
  VOICE_STALE:['This recording result is no longer current','Use the latest ready transcript or record a new message.'],
  VOICE_PENDING:['A recording still needs attention','Wait for recognition, then use or discard the transcript before continuing.'],
  SPOKEN_AMOUNT:['Please confirm the spoken amount','Check the amount shown in the conversation and confirm it, or correct it before reviewing.'],
  MIC_DENIED:['Microphone access was denied','Allow microphone access in your browser settings and try again, or type your request.'],
  MIC_UNAVAILABLE:['The microphone is unavailable','Check that a microphone is connected and selected, or type your request.'],
  ASR_EMPTY:['No speech was recognized','Check the microphone, speak clearly and record again, or type your request.'],
  HISTORY_REMOVED:['This conversation was removed','Refresh history. You can restore a deleted conversation in Settings → Deleted conversations.'],
  HISTORY_CHANGED:['The conversation has changed','Refresh history and check the latest state before repeating the action.'],
  HISTORY_BUSY:['This conversation has an unfinished operation','Finish the current recording, review or pending request before changing its history entry.'],
  HISTORY_CONFIRMATION:['The history confirmation is invalid','Reopen the requested history action and confirm the current conversation.'],
  HISTORY_EXPIRED:['The history confirmation expired','Reopen the requested history action and confirm it again.'],
  TITLE:['The conversation name is invalid','Enter a nonempty name of up to 80 characters.'],
  TITLE_CONFLICT:['The name was changed in another window','Refresh history and check the latest name before renaming again.'],
  STALE_TURN:['A newer message replaced this request','Check the latest conversation reply before sending another correction.'],
  TURN_COMPLETE:['This request was already processed','Read the saved reply in the conversation before sending it again.'],
  LOCKED:['This record can no longer be changed','It has already completed or been cancelled. Check its saved status and receipt.'],
  INPUT:['Some request details are invalid','Check the entered values and complete any required fields, then try again.'],
  SCHEMA:['The request format is invalid','Reload the app and try again. If it repeats, check that the frontend and server versions match.'],
  RESPONSE_INVALID:['The app returned an unreadable response','Reload the app and check the server connection. If a transfer was submitted, check its original result first.'],
  FORBIDDEN:['The request was not permitted','Reload the demo page to refresh access, then try again.'],
  NOT_FOUND:['The requested record was not found','Refresh the list and open an available conversation or transfer.'],
  BUSY:['The app is temporarily busy','Wait for the current operation to finish, then try again.'],
  CONFLICT:['The operation conflicts with the latest state','Refresh and check the current conversation or transfer before trying again.'],
  STATE:['This action is unavailable in the current state','Check the latest conversation and finish any pending steps before continuing.'],
  DISABLED:['This feature is disabled','Check the app configuration or use another available input method.'],
  SERVER:['The app could not complete the request','Check the saved state and server logs using this reference. If a transfer was submitted, check its original result before retrying.'],
  ALREADY_RUNNING:['The app is already running','Use the existing app instance or stop it before starting another.'],
  PORT_UNAVAILABLE:['The requested port is unavailable','Check which process is using that port or start the app on an available port.'],
  UNKNOWN:['An unexpected problem occurred','The app did not provide a recognized error type. Check the current state and use this reference with the server logs. If a transfer was submitted, check its original result first.']
});
function issueText(code,detail=''){
  let [reason,next]=ISSUE_HELP[Object.hasOwn(ISSUE_HELP,code)?code:'UNKNOWN'];
  // Extract only the exact monetary limit from the host's known eligibility notice.
  // Never forward an arbitrary server/provider diagnostic into the UI.
  const limit=code==='SINGLE_LIMIT' && /^This amount exceeds the mock single-transfer limit of (SGD [\d,]+\.\d{2})\./.exec(detail);
  if(limit)reason=`The transfer exceeds the ${limit[1]} single-payment limit`;
  return `${reason}\n${next}\nReference: ${issueCode(code)}`;
}
function issueError(code,detail='') {const failure=new Error(issueText(code));failure.code=code;failure.detail=detail;return failure;}
function validationIssue(result){return ['DISCREPANCY','NEEDS_CLARIFICATION','STALE'].includes(result.decision)?result.decision:result.code||'CHECK_ERROR';}
function error(message='') { $('error').textContent = message; $('error').hidden = !message; }
async function api(path, body, raw=false) {
  const headers = {'X-DCTA-Token': token};
  const controller=new AbortController(); const timeout=setTimeout(()=>controller.abort(),65000);
  const options = {headers, cache:'no-store',signal:controller.signal};
  if (body !== undefined) {
    options.method = 'POST';
    headers['Content-Type'] = raw ? (body.type || 'audio/wav') : 'application/json';
    options.body = raw ? body : JSON.stringify(body);
  }
  try {
    let response;
    try {response=await fetch(path,options);}catch(e){if(e.name==='AbortError')throw e;throw issueError('NETWORK_ERROR');}
    const httpCode=({400:'INPUT',401:'FORBIDDEN',403:'FORBIDDEN',404:'NOT_FOUND',409:'CONFLICT',429:'BUSY'})[response.status]||'SERVER';
    let data;
    try {data=await response.json();}catch(e){if(e.name==='AbortError')throw e;const failure=issueError(response.ok?'RESPONSE_INVALID':httpCode);failure.status=response.status;throw failure;}
    if (!response.ok) {
      const failure=issueError(data?.error||httpCode,data?.message||'');failure.status=response.status;
      if(failure.code==='SECURITY_PAUSE'){const seconds=Number((/(\d+) more seconds/.exec(failure.detail)||[])[1]||10);try{securityPause(seconds*1000);}catch{}}
      throw failure;
    }
    if(body!==undefined)save('dcta-change',crypto.randomUUID());
    return data;
  } catch(e) {
    if(e.name==='AbortError') throw issueError('REQUEST_TIMEOUT');
    throw e;
  } finally {clearTimeout(timeout);}
}
// Plain-language labels and a visual tone per reply kind (the raw kind stays in the data, not on screen).
const KIND_LABELS={TEXT:'Typed','VOICE TRANSCRIPT':'Voice',CLARIFY:'Question',DRAFT:'Draft ready',BALANCE:'Balance',PAYEES:'Saved payees',HISTORY:'Transfer history',
  ESTIMATE:'Estimate',CANCELLED:'Cancelled',ERROR:'Could not do that',UNSUPPORTED:'Not supported',INFO:'Info',PENDING:'In progress',ALREADY_COMPLETED:'Already settled',GREETING:'Hello'};
const KIND_TONES={CLARIFY:'question',DRAFT:'draft',BALANCE:'info',PAYEES:'info',HISTORY:'info',ESTIMATE:'info',CANCELLED:'muted',ERROR:'problem',UNSUPPORTED:'problem',ALREADY_COMPLETED:'done'};
function message(who, content, kind='', candidates=[], createdAt) {
  const item = document.createElement('div'); item.className = `message ${who === 'You' ? 'you' : 'dcta tone-'+(KIND_TONES[kind]||'plain')}`;
  const label = document.createElement('div'); label.className = 'who'; label.textContent = who.toUpperCase() + (kind ? ` · ${(KIND_LABELS[kind]||kind.replace(/_/g,' ').toLowerCase()).toUpperCase()}` : '');
  const bubble = document.createElement('div'); bubble.className = 'bubble';
  if(who!=='DCTA' || (!renderPayeesReply(bubble,content,kind) && !renderPlanReply(bubble,content,kind,createdAt)))bubble.textContent=content;
  item.append(label, bubble);
  if(candidates.length) {
    const choices=document.createElement('div'); choices.className='suggestions';
    for(const candidate of candidates) { const choice=document.createElement('button'); choice.className='secondary'; choice.textContent=candidate.name+(candidate.ending ? ` · ${candidate.ending}` : ''); choice.onclick=()=>send(candidate.name).catch(e=>error(e.message)); choices.append(choice); }
    item.append(choices);
  }
  $('history').append(item);
}
// While the model is thinking, show the sent text and a neutral placeholder. The model's reasoning is never displayed.
function showWaiting(text,kind='TEXT'){
  hideWaiting();awaitingReply=true;try{updateSteps();}catch{}
  $('chatPanel').classList.remove('is-empty');
  if(text)message('You',text,kind);
  const item=document.createElement('div');item.className='message waiting';item.id='waitingReply';
  const label=document.createElement('div');label.className='who';label.textContent='DCTA';
  const bubble=document.createElement('div');bubble.className='bubble';bubble.setAttribute('role','status');bubble.setAttribute('aria-live','polite');
  const words=document.createElement('span');words.textContent='Waiting for response';
  const dots=document.createElement('span');dots.className='waiting-dots';dots.setAttribute('aria-hidden','true');dots.textContent='……';
  bubble.append(words,dots);item.append(label,bubble);$('history').append(item);
  try{$('history').scrollTop=$('history').scrollHeight;}catch{}
}
function hideWaiting(){awaitingReply=false;try{document.getElementById('waitingReply')?.remove?.();}catch{}try{updateSteps();}catch{}}
function money(minor) { return `SGD ${Math.floor(minor/100).toLocaleString('en-SG')}.${String(minor%100).padStart(2,'0')}`; }
// CV14 transaction look (like a bank's transaction list): a category icon, "SGD" small, the number large.
const CATEGORY_ICON={'Transfer':'transfer','Family & friends':'family','Rent':'home','Security deposit':'key','Shopping':'basket','Bills & utilities':'bill',
  'Education':'education','Medical':'medical','Travel':'travel','Investment':'investment','Salary':'salary','Others':'other'};
const TYPE_LABEL={PERSON:'Funds transfer',OWN:'Own account transfer',SHARES:'Share purchase (mock)',BILL:'Bill payment'};
function categoryIcon(category,kind){if(kind==='OWN' && (!category||category==='Transfer'))return 'own';return CATEGORY_ICON[category]||'transfer';}
function setIcon(img,category,kind){if(img)img.src=`/icons/cat-${categoryIcon(category,kind)}.svg`;}
function iconCircle(category,kind,big=false){const wrap=document.createElement('span');wrap.className='txn-icon-circle'+(big?' big':'');
  const img=document.createElement('img');img.alt='';img.width=big?28:22;img.height=big?28:22;setIcon(img,category,kind);wrap.append(img);return wrap;}
function setAmount(el,minor,sign=''){
  // Text stays "SGD 1,000.00" (or "SGD −1,000.00"); only the look changes.
  const cur=document.createElement('span');cur.className='cur';cur.textContent='SGD ';
  const num=document.createElement('span');num.className='num';num.textContent=sign+money(minor).slice(4);el.replaceChildren(cur,num);
}
function categoryOf(purposes,defaults,i){return purposes?.[i]?.category || defaults?.[i] || 'Transfer';}
// Category field of a combined payment: one line per part, each with its own icon (bill icon for bills, arrows for transfers).
function categoryLines(iconId,textId,info,n){
  const icon=$(iconId),text=$(textId);if(icon)icon.hidden=true;text.classList?.add('cat-lines');
  text.replaceChildren(...Array.from({length:n},(_,i)=>{const cat=categoryOf(info.purposes,info.defaults,i),line=document.createElement('span');line.className='cat-line';
    const img=document.createElement('img');img.alt='';img.width=20;img.height=20;setIcon(img,cat,info.kinds?.[i]);
    const label=document.createElement('span');label.textContent=`${i+1}. ${purposeLabel(info.purposes?.[i],cat)}`;line.append(img,label);return line;}));
}
// Hero of a combined payment: one small icon per part, at most 3, then "…" for the rest.
const HERO_ICONS_MAX=3;
function heroIcons(pre,info,n){
  const circle=$(pre+'IconCircle'),stack=$(pre+'IconStack'),many=n>1;
  if(circle)circle.hidden=many;if(!stack)return;stack.hidden=!many;
  if(!many){stack.replaceChildren();return;}
  const shown=Array.from({length:Math.min(n,HERO_ICONS_MAX)},(_,i)=>iconCircle(categoryOf(info.purposes,info.defaults,i),info.kinds?.[i]));
  if(n>HERO_ICONS_MAX){const more=document.createElement('span');more.className='txn-icon-circle more';more.textContent='…';shown.push(more);}
  stack.replaceChildren(...shown);
}
function categorySingle(iconId,textId,label){const icon=$(iconId);if(icon)icon.hidden=false;$(textId).classList?.remove('cat-lines');$(textId).textContent=label;}
function renderParts(listId,actions,info){
  // Each payment of a combined payment as its own small row: icon, recipient, category, amount.
  const list=$(listId);const many=Array.isArray(actions)&&actions.length>1;list.hidden=!many;
  list.replaceChildren(...(many?actions.map((a,i)=>{const li=document.createElement('li');li.className='txn-part';
    const cat=categoryOf(info.purposes,info.defaults,i);
    const body=document.createElement('span');body.className='txn-part-body';
    const name=document.createElement('strong');name.textContent=`${a.payee.name} · ending ${a.payee.ending}`;
    const sub=document.createElement('small');sub.textContent=`${purposeLabel(info.purposes?.[i],cat)} · ${TYPE_LABEL[info.kinds?.[i]]||TYPE_LABEL.PERSON}`;
    body.append(name,sub);const amount=document.createElement('span');amount.className='txn-part-amount';setAmount(amount,a.amount_minor);
    li.append(iconCircle(cat,info.kinds?.[i]),body,amount);return li;}):[]));
  return many;
}
// Multi-transfer plans: every action is listed in order; the plan is authorized and executed as one unit.
function renderPlan(listId,noteId,actions,constraints,describe) {
  const list=$(listId),note=$(noteId),keep=Array.isArray(constraints)&&constraints.length>0;
  const plan=(Array.isArray(actions)&&actions.length>1)||keep;
  list.hidden=!plan;note.hidden=!plan;
  list.replaceChildren(...(plan?actions.map((action,index)=>{const item=document.createElement('li');item.textContent=describe(action,index);return item;}):[]));
  note.textContent=!plan?'':(keep?`Keeps at least ${money(constraints[0].amount_minor)} in ${constraints[0].account_name||constraints[0].account_id}. `:'')+'All transfers run together or not at all.';
  return plan;
}
// Optional read-back. Stop invalidates pending synthesis as well as playing audio.
let speakNext=false;
let readbackEpoch=0,readbackController=null,readbackUrl='';
function readAloudOn(){try{return localStorage.getItem('dcta.readAloud')==='on';}catch{return false;}}
function stopReadback(){
  readbackEpoch++;readbackController?.abort();readbackController=null;
  try{window.speechSynthesis?.cancel();}catch{}
  const audio=$('readbackAudio');audio.onended=null;audio.onerror=null;audio.pause();audio.removeAttribute('src');
  if(readbackUrl)URL.revokeObjectURL(readbackUrl);readbackUrl='';
  $('readbackBar').hidden=true;$('readbackPlay').hidden=true;$('readbackPlay').onclick=null;
}
function speechSegments(text){
  // Prefer sentence/word boundaries; never silently truncate transfer details.
  const result=[];let rest=String(text).replace(/SGD /g,'S G D ').replace(/\s+/g,' ').trim();
  if(rest.length>6000)throw Error('This reply is too long to read aloud. Please read the complete details on screen.');
  while(rest){
    let end=Math.min(240,rest.length);
    if(end<rest.length){
      const head=rest.slice(0,end),sentences=[...head.matchAll(/[.!?;] +/g)];
      const boundary=sentences.length?sentences[sentences.length-1].index+1:head.lastIndexOf(' ');
      if(boundary>0)end=boundary;
    }
    result.push(rest.slice(0,end).trim());rest=rest.slice(end).trim();
  }
  return result;
}
function readbackStatus(text){$('readbackStatus').textContent=text;$('readbackBar').hidden=false;}
async function cloudReadback(text,epoch,sid){
  const valid=()=>epoch===readbackEpoch && session===sid && readAloudOn();
  try{
    const segments=speechSegments(text),audio=$('readbackAudio');
    async function next(index){
      if(!valid())return;
      if(index>=segments.length){stopReadback();return;}
      readbackStatus(`Preparing Tencent voice${segments.length>1?` · ${index+1}/${segments.length}`:''}…`);
      $('readbackPlay').hidden=true;
      const controller=new AbortController();readbackController=controller;
      const timer=setTimeout(()=>controller.abort(),35000);
      let blob;
      try{
        const response=await fetch('/api/speech',{method:'POST',cache:'no-store',signal:controller.signal,
          headers:{'X-DCTA-Token':token,'Content-Type':'application/json'},body:JSON.stringify({session:sid,text:segments[index]})});
        if(!valid())return;
        if(!response.ok){const data=await response.json();throw Error(data.message || 'Speech is unavailable. Please read the reply on screen.');}
        blob=await response.blob();
        if(blob.type!=='audio/mpeg' || !blob.size)throw Error('Speech audio could not be loaded. Please read the reply on screen.');
      } finally {clearTimeout(timer);if(readbackController===controller)readbackController=null;}
      if(!valid())return;
      if(readbackUrl)URL.revokeObjectURL(readbackUrl);
      readbackUrl=URL.createObjectURL(blob);audio.src=readbackUrl;
      audio.onended=()=>{if(valid())next(index+1).catch(failed);};
      audio.onerror=()=>{if(valid())failed(Error('This browser could not play the speech audio. Please read the reply on screen.'));};
      async function play(){
        if(!valid())return;
        try{await audio.play();if(!valid())return;$('readbackPlay').hidden=true;
          readbackStatus(`Reading aloud · Tencent${segments.length>1?` · ${index+1}/${segments.length}`:''}`);
        }catch{if(valid()){readbackStatus('Audio is ready. Select Play audio to listen.');$('readbackPlay').hidden=false;}}
      }
      $('readbackPlay').onclick=play;
      await play();
    }
    function failed(e){if(valid()){stopReadback();readbackStatus(e.name==='AbortError'?'Speech timed out. Please read the reply on screen.':e.message);$('readbackStop').textContent='Dismiss';}}
    await next(0).catch(failed);
  }catch(e){if(valid()){stopReadback();readbackStatus(e.message);$('readbackStop').textContent='Dismiss';}}
}
function speak(text){
  stopReadback();
  if(!readAloudOn()||!text)return;
  $('readbackStop').textContent='Stop reading';
  // Controls must remain inside the modal top layer while reviewing a payment.
  ($('reviewDialog').open?$('reviewDialog'):$('chatPanel')).append($('readbackBar'));
  if(config?.tts_provider==='trtc'){void cloudReadback(text,readbackEpoch,session);return;}
  try{if(!window.speechSynthesis)return;
    const epoch=readbackEpoch,utterance=new SpeechSynthesisUtterance(String(text).replace(/SGD /g,'S G D '));
    utterance.lang='en-SG';utterance.rate=1;utterance.onend=()=>{if(epoch===readbackEpoch)stopReadback();};
    utterance.onerror=()=>{if(epoch===readbackEpoch)stopReadback();};
    readbackStatus('Reading aloud · browser voice');window.speechSynthesis.speak(utterance);}catch{stopReadback();}
}
$('readbackStop').onclick=stopReadback;
function spokenReview(r){
  const lines=(r.actions&&r.actions.length>1?r.actions:[{amount_minor:r.amount_minor,payee:r.payee,source:r.source}])
    .map((a,i,all)=>`${all.length>1?`Transfer ${i+1}: `:''}${money(a.amount_minor)} to ${a.payee.name}, account ending ${a.payee.ending.split('').join(' ')}, from ${a.source.name}.`);
  const keep=(r.constraints||[]).length?` Keep at least ${money(r.constraints[0].amount_minor)} in ${r.constraints[0].account_name}.`:'';
  return `Please check before approving. ${lines.join(' ')} Total ${money(r.total_minor)}.${keep}`;
}
function planDescribe(action,index){return `${index+1}. ${money(action.amount_minor)} to ${action.payee.name} · ending ${action.payee.ending} — from ${action.source.name}`;}
function renderChanges(diff) {
  $('draftChanges').hidden=!diff || !diff.changes.length;
  $('draftChangeList').replaceChildren();
  if(!diff || !diff.changes.length)return;
  $('draftChangeTitle').textContent=`Changed from version ${diff.from_version} to ${diff.to_version}`;
  for(const change of diff.changes) {
    const row=document.createElement('div');row.className='draft-change';
    const label=document.createElement('strong');label.textContent=change.label;
    const before=document.createElement('p');before.textContent=`Before: ${change.before}`;
    const after=document.createElement('p');after.textContent=`Now: ${change.after}`;
    row.append(label,before,after);$('draftChangeList').append(row);
  }
}
// Visual summary of the current draft or the latest receipt at the end of the conversation.
// It only opens the existing detail dialogs; approval still goes through review + device verification.
function cardNode(tag,className,text){const node=document.createElement(tag);node.className=className;if(text!==undefined)node.textContent=text;return node;}
// Format the host's saved destinations as compact, readable category lists.
function renderPayeesReply(bubble,text,kind){
  if(kind!=='PAYEES' || typeof text!=='string')return false;
  const titles=new Set(['Transfers','Own accounts','Investment','Bills']);
  const blocks=text.split('\n\n').map(block=>block.split('\n'));
  const isGroup=([title,...rows])=>titles.has(title) && rows.length>0 && rows.every(row=>row.startsWith('• '));
  if(!blocks.some(isGroup))return false;
  bubble.classList.add('payees-reply');
  for(const block of blocks){
    if(isGroup(block)){
      const group=cardNode('section','payees-group'),list=cardNode('ul','');
      group.append(cardNode('h3','',block[0]));
      for(const row of block.slice(1))list.append(cardNode('li','',row.slice(2)));
      group.append(list);bubble.append(group);
    }else bubble.append(cardNode('p','',block.join('\n')));
  }
  return true;
}
// Reformat only complete, host-authored multi-payment summaries. Keep every original
// value and trailing notice; old turns must never borrow details from the current draft.
function renderPlanReply(bubble,text,kind,createdAt){
  if(!['DRAFT','CANCELLED'].includes(kind) || typeof text!=='string')return false;
  const head=/^((?:Cancelled draft|Draft) v\d+): ([1-9]\d*) (transfers|payments) — ([\s\S]+)$/.exec(text);
  if(!head || Number(head[2])<2)return false;
  const tail=kind==='DRAFT'?/; (total SGD [\d,]+\.\d{2})(\.[\s\S]+)$/.exec(head[4]):/(\. No money moved\.[\s\S]*)$/.exec(head[4]);
  if(!tail)return false;
  let body=head[4].slice(0,tail.index),constraint='';
  const keep=/(, keeping at least SGD [\d,]+\.\d{2})$/.exec(body);
  if(keep){constraint=keep[1].slice(2);body=body.slice(0,keep.index);}
  const parts=body.split('; ');
  if(parts.length!==Number(head[2]) || parts.some(part=>!/^SGD [\d,]+\.\d{2} .+ from .+$/.test(part)))return false;
  bubble.classList.add('plan-reply');
  const top=cardNode('div','plan-reply-heading');
  top.append(cardNode('strong','',head[1]),cardNode('span','',`${head[2]} ${head[3]}`));
  const list=cardNode('ol','payment-grid');
  for(const part of parts){
    const amount=/^(SGD [\d,]+\.\d{2}) (.+)$/.exec(part);
    const transfer=/^to (.+) \(ending (\d+)\) from (.+)$/.exec(amount[2]);
    if(transfer)list.append(paymentTile(amount[1],{name:transfer[1],ending:transfer[2]},{name:transfer[3]},createdAt));
    else {
      // Keep specialized investment/bill explanations intact, including calculations.
      const entry=cardNode('li','payment-tile');
      entry.append(cardNode('strong','payment-amount',amount[1]),cardNode('span','payment-description',amount[2]));
      appendPaymentDate(entry,createdAt);list.append(entry);
    }
  }
  bubble.append(top,list);
  if(constraint)bubble.append(cardNode('p','plan-note',constraint.charAt(0).toUpperCase()+constraint.slice(1)));
  if(kind==='DRAFT')bubble.append(cardNode('p','plan-total',tail[1].replace(/^total /,'Total ')));
  bubble.append(cardNode('p','plan-note',tail[kind==='DRAFT'?2:1].slice(2)));
  return true;
}
function appendPaymentDate(row,value){
  if(value===null || value===undefined || value==='')return;
  const label=formatWhen(value);if(!label)return;
  const date=cardNode('time','payment-date',label);
  date.setAttribute('datetime',new Date(typeof value==='string' && /^\d+$/.test(value)?Number(value):value).toISOString());
  row.append(date);
}
function paymentTile(amount,payee,source,createdAt,{showAmount=true}={}){
  const row=cardNode('li','payment-tile');
  if(showAmount)row.append(cardNode('strong','payment-amount',typeof amount==='string'?amount:money(amount)));
  else row.classList.add('payment-tile-inline');
  const line=cardNode('div','payment-summary');
  line.append(cardNode('strong','payment-name',payee.name||''));
  const detail=(payee.ending?` - Ending ${payee.ending}`:'')+(source?.name?` - Transfer from ${source.name}`:'');
  if(detail)line.append(cardNode('span','payment-detail',detail));
  row.append(line);appendPaymentDate(row,createdAt);
  return row;
}
function statusCard(state,draft,which) {
  // which: 'draft' builds the current-draft card, 'receipt' the latest receipt card. Returns the node (or null).
  const receipt=which==='receipt' && config?.payments_ready ? state.receipt : null;
  if(which==='receipt')draft=null;
  if(!draft && !receipt)return null;
  const lookup=(list,id)=>(list||[]).find(x=>x.id===id);
  const item=cardNode('div','message dcta card-message');
  const card=cardNode('section','flow-card');
  const lines=cardNode('ul','card-lines');
  let eyebrow,title,amount,note='',buttonText,open;
  if(draft) {
    const blocked=!!state.eligibility?.blocked;
    const acts=draft.payload.actions||[];
    if(acts.length>1)lines.classList.add('payment-grid');
    card.className=`flow-card ${blocked?'blocked':'draft'}`;
    eyebrow=blocked?'Blocked · change needed':'Draft · nothing sent yet';title=`Version ${draft.current_version}`;
    amount=money(acts.length>1?draft.payload.total_minor:acts[0].amount_minor);
    for(const [index,act] of acts.entries()) {
      const payee=lookup(state.payees,act.payee_id)||{name:act.payee_id,ending:'----'},source=lookup(state.accounts,act.source_account_id)||{name:act.source_account_id};
      lines.append(paymentTile(act.amount_minor,payee,source,state.draft_created_at,{showAmount:acts.length>1}));
    }
    if(state.purpose||purposeWorthShowing(state.purposes,state.purpose_defaults))lines.append(cardNode('li','card-purpose',`Purpose: ${purposeSummary(state.purposes||[state.purpose],state.purpose_defaults)}`));
    const keep=(draft.payload.constraints||[])[0];
    if(blocked)note='Blocked by a safety rule — see the notice above. Change the amount or cancel this draft.';
    else if(keep)note=`Keeps at least ${money(keep.amount_minor)} in ${(lookup(state.accounts,keep.account_id)||{}).name||keep.account_id}. All transfers run together or not at all.`;
    else if(!state.eligibility?.stepup)note='Next: an independent check, then you approve on this device.';
    buttonText=blocked?'View blocked draft':'Review & approve';open=()=>$('viewDraft').onclick();
    const heard=blocked?null:(state.amount_checks||[])[0];
    if(heard) {
      // CV14 D: a spoken amount that is easy to mishear (fifteen / fifty) is confirmed once before review.
      card.className='flow-card changed';eyebrow='Voice · please check the amount';
      const warn=cardNode('div','card-warning spoken-check');warn.setAttribute('role','alert');
      warn.append(cardNode('strong','',`🎙 I heard ${heard.display}`),cardNode('p','spoken-words',`That is ${heard.spoken}. Is that the amount you said?`));
      const yes=cardNode('button','',`Yes, ${heard.display}`);yes.type='button';
      yes.onclick=()=>confirmSpokenAmount(heard,yes);
      const no=cardNode('button','secondary','No, change it');no.type='button';
      no.onclick=()=>{const box=$('message');box.value='Make it SGD ';box.focus();box.setSelectionRange?.(box.value.length,box.value.length);box.dispatchEvent?.(new Event('input'));};
      const row=cardNode('div','card-actions');row.append(no,yes);warn.append(row);
      const top=cardNode('div','card-top');top.append(cardNode('span','card-eyebrow',eyebrow),cardNode('span','card-pill',title));
      card.replaceChildren(top,cardNode('p','card-amount',amount),lines,warn);
      card.setAttribute('aria-label',`${eyebrow}: ${amount}`);
      item.append(cardNode('div','who','DCTA · CURRENT DRAFT'),card);return item;
    }
    if(!blocked && changeToConfirm()) {
      // Double check before a changed amount or payee can be reviewed.
      card.className='flow-card changed';eyebrow='Changed · please double-check';
      const warn=cardNode('div','card-warning');warn.setAttribute('role','alert');
      warn.append(cardNode('strong','','⚠ The money details changed. Is this what you meant?'));
      const list=cardNode('ul','');
      for(const change of currentChanges)list.append(cardNode('li','',`${change.label}: ${change.before} → ${change.after}`));
      warn.append(list);
      const confirm=cardNode('button','',`Yes, use the new details`);confirm.type='button';
      confirm.onclick=()=>{confirmedChanges.add(changeKey());if(lastRenderedState)render({...lastRenderedState,state_sequence:undefined});$('openCurrentDraft')?.focus();};
      const undo=cardNode('button','secondary','No, change it back');undo.type='button';
      undo.onclick=()=>{const text=currentChanges.map(c=>`change the ${c.label.toLowerCase()==='to'?'payee':c.label.toLowerCase()} back to ${c.before.replace(/ · ending \d+$/,'').replace(/ · \w+$/,'')}`).join(' and ');
        send(text.charAt(0).toUpperCase()+text.slice(1)+'.').catch(e=>error(e.message));};
      const row=cardNode('div','card-actions');row.append(undo,confirm);warn.append(row);
      const top=cardNode('div','card-top');top.append(cardNode('span','card-eyebrow',eyebrow),cardNode('span','card-pill',title));
      card.replaceChildren(top,cardNode('p','card-amount',amount),lines,warn);
      card.setAttribute('aria-label',`${eyebrow}: ${amount}`);
      item.append(cardNode('div','who','DCTA · CURRENT DRAFT'),card);return item;
      return;
    }
  } else {
    // Settled: a thin card as wide as the reply bubbles: each line, the date and a green View receipt.
    const acts=receipt.actions&&receipt.actions.length?receipt.actions:[{amount_minor:receipt.amount_minor,payee:receipt.payee,source:receipt.source}];
    card.className='flow-card done compact';item.classList?.add('receipt-message');
    if(acts.length>1)lines.classList.add('payment-grid');
    for(const act of acts)lines.append(paymentTile(act.amount_minor,act.payee||{},act.source,receipt.created_ms));
    const total=money(acts.reduce((sum,a)=>sum+(a.amount_minor||0),0)||receipt.amount_minor);
    const foot=cardNode('div','card-foot');const button=cardNode('button','',`View receipt`);button.type='button';button.onclick=()=>$('viewReceipt').onclick();
    foot.append(button);
    card.append(lines,foot);card.setAttribute('aria-label',`Settled transfer: ${total}`);
    item.append(cardNode('div','who','DCTA · RECEIPT'),card);return item;
  }
  const top=cardNode('div','card-top');top.append(cardNode('span','card-eyebrow',eyebrow),cardNode('span','card-pill',title));
  const actions=cardNode('div','card-actions');const button=cardNode('button','secondary',buttonText);button.id='openCurrentDraft';button.type='button';button.onclick=open;actions.append(button);
  card.append(top,cardNode('p','card-amount',amount),lines);if(note)card.append(cardNode('p','card-note',note));card.append(actions);
  card.setAttribute('aria-label',`${eyebrow}: ${amount}`);
  item.append(cardNode('div','who',draft?'DCTA · CURRENT DRAFT':'DCTA · RECEIPT'),card);return item;
}
// One quiet status line ("…… Checking independently ……") shown only while something is happening.
// Purely informative; it never enables an action. Hidden when the conversation is idle.
let awaitingReply=false,doneUntil=0,doneTimer=null,stepup=null,pauseTimer=null,pauseUntil=0,successTimer=null;
const receiptSeen=new Map();
function flowStage() {
  if(stepup)return 'Enter your security code';
  if(signing || pendingPayment)return 'Sending the mock transfer';
  if(review)return 'Approve on your device';
  if(reviewing)return 'Checking independently';
  // Only while a message or transcript is actually on its way; never on an idle or freshly opened conversation.
  if(awaitingReply && (sendingText || turnsInFlight>0 || voice?.status==='SUBMITTED'))return 'Understanding your request';
  if(currentDraft) {
    if(reviewGate && reviewGate.code==='SPOKEN_AMOUNT')return 'Confirm the amount you said';
    if(reviewGate && reviewGate.ready===false && reviewGate.code)return 'Blocked · change or cancel the draft';
    if(changeToConfirm())return 'Confirm the changed details';
    return 'Draft ready · review when you are ready';
  }
  if(Date.now()<doneUntil)return 'Done';
  return '';
}
function updateSteps() {
  const box=$('flowStatus');if(!box)return;
  const text=flowStage();
  box.hidden=!text;$('flowStatusText').textContent=text;
  box.className=`flow-status${text==='Done'?' done':''}${text.startsWith('Blocked')||text.startsWith('Confirm')?' attention':''}`;
}
function noteReceipt(state) {
  // "Done" flashes only for a receipt that appears while the user is in this conversation, not for old ones.
  const id=state.receipt?.execution_id||null,known=receiptSeen.has(state.session_id),previous=receiptSeen.get(state.session_id);
  receiptSeen.set(state.session_id,id);
  if(known && id && previous!==id){doneUntil=Date.now()+6000;clearTimeout(doneTimer);doneTimer=setTimeout(updateSteps,6100);}
}
// Money-moving corrections (amount or payee) must be confirmed before a review can start.
const confirmedChanges=new Set();
function changeKey(){return currentDraft?`${currentDraft.payload.draft_id}:${currentDraft.current_version}:${currentDraft.content_hash}`:'';}
function importantChanges(diff){return (diff?.changes||[]).filter(c=>/(^|\s)(amount|to)$/i.test(c.label));}
let currentChanges=[],lastRenderedState=null;
function changeToConfirm(){return currentChanges.length>0 && !confirmedChanges.has(changeKey());}
function render(state) {
  if (state.session_id !== session) return;
  if(state.security_pause_ms>0 && Date.now()+state.security_pause_ms>pauseUntil+500)securityPause(state.security_pause_ms);
  if(Number.isInteger(state.state_sequence)) {
    if(state.state_sequence<lastStateSequence)return;
    lastStateSequence=state.state_sequence;
  }
  $('sessionHint').textContent = state.revision ? `${state.revision} ${state.revision===1?'message':'messages'}` : 'Start a conversation';
  $('chatTitle').textContent=state.custom_title || 'Conversation';
  $('chatTitle').setAttribute('title',state.custom_title || 'Conversation');
  $('history').replaceChildren();
  $('chatPanel').classList.toggle('is-empty',!state.history.length && !state.draft && !state.receipt);
  if (!state.history.length) {
    const welcome = document.createElement('div'); welcome.className = 'welcome';
    // Quick actions use the normal send path without replacing an unfinished message.
    const mark = document.createElement('img'); mark.className = 'welcome-mark'; mark.src = '/icons/logo.svg'; mark.alt = ''; mark.width = 44; mark.height = 44;
    const title = document.createElement('h3'); title.textContent = 'How can I help you today?';
    const choices = document.createElement('div'); choices.className = 'suggestions';
    for (const label of ['Check balance','Prepare a transfer','What can you do?']) {
      const button = document.createElement('button'); button.type = 'button'; button.className = 'secondary'; button.textContent = label;
      button.onclick = () => send(label); choices.append(button);
    }
    welcome.append(mark,title,choices); $('history').append(welcome);
  }
  const draft = state.draft;
  lastRenderedState=state;currentDraft=draft;currentChanges=draft?importantChanges(state.draft_changes):[];
  // Cards sit where they happened: the draft card after the message that created its current version,
  // the receipt card after the last message before the transfer executed (not at the end of later chat).
  const cards=[['draft',state.draft_anchor],['receipt',state.receipt_anchor]].map(([which,anchor])=>({which,anchor,node:statusCard(state,draft,which)})).filter(c=>c.node);
  const ordinals=state.history.map(t=>t.ordinal).filter(Number.isInteger);
  const placeCard=card=>$('history').append(card.node);
  for(const card of cards)if(Number.isInteger(card.anchor) && ordinals.length && card.anchor<ordinals[0])placeCard(card);
  for (const turn of state.history) {
    message('You',turn.text,turn.voice_id ? 'VOICE TRANSCRIPT' : 'TEXT');
    const reply=turn.response.kind==='ERROR'?issueText(turn.response.code):turn.response.message||turn.response.kind;
    const currentDraftReply=turn.response.kind==='DRAFT' && Number.isInteger(turn.ordinal) && cards.some(c=>c.which==='draft' && c.anchor===turn.ordinal);
    if(currentDraftReply && !state.eligibility?.blocked && !(state.amount_checks||[]).length && !changeToConfirm() && !(turn.response.candidates||[]).length) {
      // Keep the complete original response available beside its structured card.
      // Historical replies and safety questions remain expanded.
      const details=cardNode('details','draft-response');
      details.append(cardNode('summary','','Draft prepared · view full response'),cardNode('div','bubble',reply));
      $('history').append(details);
    } else message('DCTA',reply,turn.response.kind,turn===state.history.at(-1) ? (turn.response.candidates || []) : [],turn.created_at);
    if(speakNext && turn===state.history.at(-1)){speakNext=false;speak(reply);}
    for(const card of cards)if(Number.isInteger(turn.ordinal) && card.anchor===turn.ordinal)placeCard(card);
  }
  for(const card of cards)if(!Number.isInteger(card.anchor) || !ordinals.length || (card.anchor>=ordinals[0] && !ordinals.includes(card.anchor)))placeCard(card);
  $('history').scrollTop = $('history').scrollHeight;
  currentDraft=draft;
  reviewGate=state.review_gate || {ready:true};
  const blocked=!!draft && !!state.eligibility?.blocked;
  if(blocked)reviewGate={ready:false,code:state.eligibility.code,message:state.eligibility.message};
  $('draftRuleNotice').hidden=!blocked;$('draftBlock').hidden=!blocked;currentPayees=state.payees||[];if(!blocked)$('draftRuleHint').hidden=true;
  $('draftRuleMessage').textContent=blocked?issueText(state.eligibility.code,state.eligibility.message):'';$('draftBlockMessage').textContent=blocked?issueText(state.eligibility.code,state.eligibility.message):'';
  $('draftState').textContent=blocked?'Blocked · action required':review?'Independently checked · waiting for your approval':'Draft · not validated';setTone($('draftState'),$('draftState').textContent);
  $('viewDraft').textContent=blocked?'View blocked draft':'Review current draft';
  if(config?.payments_ready) {
    if(!pendingPayment) {
      const recovered=(state.recoverable_requests || []).find(x=>x.status==='PENDING' && validPending(x,session));
      if(recovered) {pendingPayment=validPending(recovered,session);pendingMessage('Recovered from the server. Check the original result or explicitly retry the same request.');}
    }
    updateReviewControls();
    if(review && (!draft || draft.content_hash!==review.review.content_hash || state.revision!==review.revision || state.voice_epoch!==review.voice_epoch || !reviewGate.ready)) {
      const changed=draft && draft.content_hash!==review.review.content_hash;
      invalidateReview();
      error(changed ? 'The draft changed. The old review is closed. Open Review current draft to check the latest changes, then review again.' : 'The conversation changed. The old review is closed. Check the current details before reviewing again.');
    }
    if(draft && !reviewGate.ready)$('reviewStatus').textContent=reviewGate.code?issueText(reviewGate.code,reviewGate.message):reviewGate.message;
    else if(draft && changeToConfirm())$('reviewStatus').textContent='The amount or payee changed. Confirm the new details in the conversation first.';
    else if(draft && !reviewing && !review && !pendingPayment)$('reviewStatus').textContent='Review runs an independent check before authorization.';
    showReceipt(state.receipt);
  }
  noteReceipt(state);
  if(!turnsInFlight && !document.getElementById('waitingReply'))awaitingReply=false;
  updateSteps();
  $('viewDraft').hidden=!draft; $('draftDetails').hidden = !draft;
  fillPurposeOptions();showPurpose(draft?state.purpose:null,draft?draft.payload.draft_id:'',draft?state:null);
  if(!draft)$('draftDialog').close();
  $('draftVersion').textContent = draft ? `VERSION ${draft.current_version}` : state.conversation.cancelled ? 'CANCELLED' : 'EMPTY';
  renderChanges(draft ? state.draft_changes : null);
  if (draft) {
    const action = draft.payload.actions[0];
    const source = state.accounts.find(x=>x.id === action.source_account_id);
    const payee = state.payees.find(x=>x.id === action.payee_id);
    $('amount').textContent = money(action.amount_minor);
    $('source').textContent = source ? `${source.name} · ${source.id}` : action.source_account_id;
    $('payee').textContent = payee ? `${payee.name} · ending ${payee.ending}` : action.payee_id;
    $('fee').textContent = money(action.fee_minor);
    const actions=draft.payload.actions.map(item=>({amount_minor:item.amount_minor,payee:state.payees.find(x=>x.id===item.payee_id)||{name:item.payee_id,ending:'----'},source:state.accounts.find(x=>x.id===item.source_account_id)||{name:item.source_account_id}}));
    const plan=renderPlan('draftActions','draftPlanNote',actions,(draft.payload.constraints||[]).map(c=>({...c,account_name:(state.accounts.find(x=>x.id===c.account_id)||{}).name})),planDescribe);
    for(const id of ['sourceLabel','source','payeeLabel','payee'])$(id).hidden=plan;
    $('amountLabel').textContent=plan && actions.length>1?`TOTAL OF ${actions.length} TRANSFERS`:'TRANSFER AMOUNT';
    if(plan && actions.length>1){$('amount').textContent=money(draft.payload.total_minor);$('payee').textContent=`${actions.length} saved payees (listed above)`;
      const sources=[...new Set(draft.payload.actions.map(x=>x.source_account_id))];if(sources.length>1)$('source').textContent='Several accounts (listed above)';}
  }
}
async function refresh() {
  if(!session){await sessions({quiet:true});return null;}
  const sid=session,scope=sessionEpoch,ticket=++refreshIssued;
  let state;
  try {state=await api(`/api/state?session=${encodeURIComponent(sid)}`);}
  catch(e){
    if(e.code==='HISTORY_REMOVED' && session===sid && sessionEpoch===scope){clearWorkspace();await sessions({quiet:true});return null;}
    throw e;
  }
  if(session!==sid || sessionEpoch!==scope || ticket!==refreshIssued)return null;
  render(state);await sessions({quiet:true});return state;
}
function recentBadge(row) {
  if(row.revision>0 && !row.last_kind)return 'Response pending';
  if(row.voice_status==='PROCESSING')return 'Recognizing audio';
  if(row.voice_status==='READY')return 'Transcript to review';
  if(row.draft_state==='SUCCEEDED')return 'Settled';  // a settled transfer stays settled, whatever was asked afterwards
  if(['ERROR','CLARIFY','RESTRICTED','UNSUPPORTED'].includes(row.last_kind))return 'Needs attention';
  if(row.draft_state==='CANCELLED' || row.last_kind==='CANCELLED')return 'Cancelled';
  if(row.draft_state==='DRAFTED')return 'Draft to review';
  return row.revision ? 'Conversation' : row.voice_status==='FAILED' ? 'Recording failed' : 'Not started';
}
function selectRecent() {
  for(const [id,button] of recentButtons)button.setAttribute('aria-current',String(id===session));
}
function renderRecents(data) {
  const scrollTop=$('recentList').scrollTop;
  const rows=data.sessions;$('recentList').replaceChildren();recentButtons.clear();recentNames.clear();
  $('recentCount').textContent=String(data.total ?? rows.length);
  for(const row of rows) {
    const button=document.createElement('button');button.type='button';button.className='recent-item';
    const title=document.createElement('span');title.className='recent-title';title.textContent=row.title || 'New conversation';
    title.setAttribute('title',title.textContent);
    recentNames.set(row.id,{title:title.textContent,version:row.title_version || 0});
    const preview=document.createElement('span');preview.className='recent-preview';preview.textContent=row.preview || 'No messages yet.';
    const meta=document.createElement('span');meta.className='recent-meta';
    const when=row.last_activity && Number.isFinite(Date.parse(row.last_activity)) ? formatWhen(row.last_activity,{compact:true}) : 'No activity yet';
    meta.textContent=when;meta.setAttribute('title',`${row.revision} ${row.revision===1?'message':'messages'} · ${when}`);
    const badge=document.createElement('span');badge.className='recent-badge';badge.textContent=recentBadge(row);setTone(badge,badge.textContent);
    button.append(title,preview,meta,badge);button.onclick=()=>switchSession(row.id).then(()=>{if(session===row.id)closeHistoryDrawer();}).catch(e=>error(e.message));
    const wrapper=document.createElement('div');wrapper.className='recent-row';
    const more=document.createElement('button');more.type='button';more.className='quiet recent-more';more.textContent='⋯';more.title='More options';
    more.setAttribute('aria-label',`Options for conversation ${row.id.slice(-8)}`);more.setAttribute('aria-haspopup','menu');
    more.onclick=()=>openSessionMenu(row.id,more);
    wrapper.oncontextmenu=e=>{e.preventDefault();openSessionMenu(row.id,more,e);};
    button.onkeydown=e=>{if(e.key==='ContextMenu' || (e.shiftKey && e.key==='F10')){e.preventDefault();openSessionMenu(row.id,more);}};
    wrapper.append(button,more);recentButtons.set(row.id,button);$('recentList').append(wrapper);
  }
  selectRecent();
  $('recentList').scrollTop=scrollTop;
  $('recentStatus').textContent=rows.length ? data.has_more ? `Showing ${rows.length} of ${data.total}. Search to find older conversations.` : '' : $('sessionSearch').value.trim() ? 'No matching conversations. Try another search.' : 'No saved conversations yet. Start a new session.';
}
function closeSessionMenu(restore=false) {
  $('sessionMenu').hidden=true;menuSession='';if(restore)menuAnchor?.focus();
}
function openSessionMenu(id,anchor,event) {
  if(deletingSession || renamingSession)return;
  menuSession=id;menuAnchor=anchor;
  const rect=anchor.getBoundingClientRect(),menu=$('sessionMenu');
  menu.style.left=`${Math.max(8,Math.min(event?.clientX ?? rect.left,window.innerWidth-206))}px`;
  menu.hidden=false;
  const height=menu.getBoundingClientRect().height || 106;
  menu.style.top=`${Math.max(8,Math.min(event?.clientY ?? rect.bottom,window.innerHeight-height-8))}px`;
  menuFocus=0;$('renameMenuItem').focus();
}
function clearWorkspace() {
  stopReadback();
  awaitingReply=false;
  sessionEpoch++;refreshIssued++;voiceEpoch++;clearTimeout(pollTimer);pendingCapture=false;stopCapture(false);voice=null;hidePlayback();
  invalidateReview();closeSessionMenu();$('draftDialog').close();$('receiptDialog').close();
  session='';currentDraft=null;pendingPayment=null;lastStateSequence=-1;switchingSession=false;
  save('dcta-session','');$('voicePanel').hidden=true;$('message').value='';pendingMessage('');
  render({session_id:'',history:[],voices:[],conversation:{},revision:0,audit_count:0,accounts:[],payees:[],draft:null,receipt:null});
  $('sessionHint').textContent='Record, upload audio or type — a conversation starts automatically';selectRecent();updateComposer();
}
function askDelete() {
  const id=menuSession;closeSessionMenu();if(!id || deletingSession)return;
  if(id===session && (sendingText || turnsInFlight || switchingSession || signing || reviewing || capture || pendingCapture || voice?.status==='SUBMITTED' || pendingPayment || review)) {
    error('Finish or close the active recording, request or review before deleting this conversation.');return;
  }
  return askHistoryChange(id,'HIDE');
}
function historyEndpoint(action) {return action==='HIDE'?'/api/session/delete':'/api/session/restore';}
function updateHistoryConfirm() {
  const target=deleteTarget;
  $('confirmDelete').disabled=!!deletingSession || !target?.challenge || Date.now()>=target.expires || (target.action==='HIDE' && !$('deleteAcknowledge').checked);
}
async function askHistoryChange(id,action) {
  if(deletingSession)return;
  clearTimeout(deletedTimer);
  const target={id,action};deleteTarget=target;
  $('deleteTitle').textContent=action==='HIDE'?'Delete conversation?':'Restore conversation?';
  $('deleteDescription').textContent=action==='HIDE'?'This removes the conversation from recent history. Original messages, recordings, transaction and audit records are kept. You can restore it from Settings → Deleted conversations. It does not cancel a transfer.':'This returns the conversation to recent history. Original messages and saved details are preserved. Restoring does not send a message, authorize or execute a transfer.';
  $('deleteTarget').textContent='Preparing confirmation…';$('deleteAcknowledge').checked=false;
  $('deleteAcknowledgement').hidden=action!=='HIDE';$('deleteExpiry').textContent='';
  $('confirmDelete').textContent=action==='HIDE'?'Delete conversation':'Restore conversation';
  $('confirmDelete').className=action==='HIDE'?'danger':'';
  $('cancelDelete').textContent=action==='HIDE'?'Keep conversation':'Cancel';
  $('deleteError').hidden=true;$('confirmDelete').disabled=true;$('cancelDelete').disabled=false;
  $('deleteDialog').showModal();$('cancelDelete').focus();
  try {
    const data=await api(historyEndpoint(action)+'/prepare',{session:id});
    if(deleteTarget!==target)return;
    if(data.session_id!==id || data.action!==action || typeof data.challenge_id!=='string' || !Number.isFinite(data.expires_ms))throw Error('Confirmation was not ready. Close this dialog and try again.');
    target.challenge=data.challenge_id;target.expires=data.expires_ms;
    $('deleteTarget').textContent=data.title || `Conversation ${id.slice(-8)}`;
    $('deleteExpiry').textContent='This confirmation is valid for two minutes. Changes in another window require a new confirmation.';
    updateHistoryConfirm();
    deletedTimer=setTimeout(()=>{if(deleteTarget===target){updateHistoryConfirm();$('deleteExpiry').textContent='Confirmation expired. Close this dialog and open it again.';}},Math.max(0,data.expires_ms-Date.now()));
  } catch(e) {if(deleteTarget===target){$('deleteError').textContent=e.message;$('deleteError').hidden=false;}}
}
async function confirmDelete() {
  updateHistoryConfirm();if($('confirmDelete').disabled)return;
  const target=deleteTarget,id=target.id;deletingSession=id;$('confirmDelete').disabled=true;$('cancelDelete').disabled=true;
  try {
    const result=await api(historyEndpoint(target.action),{session:id,challenge_id:target.challenge});
    if(result.session_id!==id || result.removed!==(target.action==='HIDE') || (target.action==='RESTORE' && result.restored!==true))throw Error('The change was not confirmed. Retry this confirmation or refresh history.');
    clearTimeout(deletedTimer);$('deleteDialog').close();deleteTarget=null;
    if(target.action==='HIDE'){editorDrafts.delete(id);if(session===id)clearWorkspace();}
    $('historyNotice').textContent=target.action==='HIDE'?'Conversation deleted from recent history. You can restore it in Settings → Deleted conversations.':'Conversation restored. Select it in recent history to continue.';
    $('historyNotice').hidden=false;
    try {await sessions();if($('deletedDialog').open)await loadDeleted();}
    catch {error('The history change was saved, but the list could not refresh. Refresh history to reload.');}
    if(target.action==='RESTORE')$('deletedStatus').focus();else $('newSession').focus();
  } catch(e) {
    $('deleteError').textContent=e.message;$('deleteError').hidden=false;
    if(['HISTORY_CHANGED','HISTORY_EXPIRED','HISTORY_CONFIRMATION'].includes(e.code))target.challenge=null;
  } finally {deletingSession='';updateHistoryConfirm();$('cancelDelete').disabled=false;}
}
$('deleteMenuItem').onclick=askDelete;$('confirmDelete').onclick=confirmDelete;
$('deleteAcknowledge').onchange=updateHistoryConfirm;
function dismissHistoryChange() {clearTimeout(deletedTimer);$('deleteDialog').close();deleteTarget=null;if($('deletedDialog').open)$('deletedStatus').focus();else menuAnchor?.focus();}
$('cancelDelete').onclick=dismissHistoryChange;
$('deleteDialog').oncancel=e=>{if(deletingSession)e.preventDefault();else dismissHistoryChange();};
async function loadDeleted() {
  const ticket=++deletedIssued,query=$('deletedSearch').value.trim();
  $('deletedList').setAttribute('aria-busy','true');$('deletedStatus').textContent='Loading deleted conversations…';
  try {
    const data=await api('/api/sessions/deleted'+(query?'?q='+encodeURIComponent(query):''));
    if(ticket!==deletedIssued || !$('deletedDialog').open)return;
    $('deletedList').replaceChildren();
    for(const row of data.sessions) {
      const item=document.createElement('div');item.className='deleted-row';
      const details=document.createElement('div');const name=document.createElement('strong');name.textContent=row.title;name.className='deleted-title';
      const info=document.createElement('p');info.className='small';
      const when=Number.isFinite(Date.parse(row.removed_at))?formatWhen(row.removed_at):'Unknown date';
      info.textContent=`${row.revision} ${row.revision===1?'message':'messages'} · Deleted ${when}`;
      const restore=document.createElement('button');restore.type='button';restore.className='secondary';restore.textContent='Restore';restore.setAttribute('aria-label',`Restore ${row.title}`);
      restore.onclick=()=>askHistoryChange(row.id,'RESTORE');details.append(name,info);item.append(details,restore);$('deletedList').append(item);
    }
    $('deletedStatus').textContent=data.sessions.length ? data.has_more?`Showing ${data.sessions.length} of ${data.total}. Search to find older conversations.`:`${data.total} deleted ${data.total===1?'conversation':'conversations'}. Restore one to return it to recent history.` : query?'No matching deleted conversations.':'No deleted conversations.';
  } catch(e) {if(ticket===deletedIssued){$('deletedStatus').textContent='Could not load deleted conversations. Previous results are kept. Try Refresh.';}throw e;}
  finally {if(ticket===deletedIssued)$('deletedList').setAttribute('aria-busy','false');}
}
function openDeleted() {$('settings').close();$('deletedDialog').showModal();return loadDeleted().catch(e=>error(e.message));}
$('openDeleted').onclick=openDeleted;
$('closeDeleted').onclick=()=>{deletedIssued++;$('deletedDialog').close();$('settingsButton').focus();};
$('deletedDialog').oncancel=()=>{deletedIssued++;$('settingsButton').focus();};
$('deletedSearchForm').onsubmit=e=>{e.preventDefault();return loadDeleted().catch(e=>error(e.message));};
$('refreshDeleted').onclick=()=>loadDeleted().catch(e=>error(e.message));
function askRename() {
  const id=menuSession,item=recentNames.get(id);closeSessionMenu();
  if(!id || !item || renamingSession)return;
  renameTarget={id,version:item.version};$('conversationName').value=item.title;
  $('renameError').hidden=true;$('saveRename').disabled=false;$('cancelRename').disabled=false;
  $('renameDialog').showModal();$('conversationName').focus();$('conversationName').select?.();
}
async function saveRename(event) {
  event.preventDefault();if(!renameTarget || renamingSession)return;
  const target=renameTarget,title=$('conversationName').value.trim();
  if(!title || Array.from(title).length>80){$('renameError').textContent='Use a name between 1 and 80 characters.';$('renameError').hidden=false;return;}
  renamingSession=target.id;$('saveRename').disabled=true;$('cancelRename').disabled=true;
  try {
    const result=await api('/api/session/rename',{session:target.id,title,expected_version:target.version});
    if(result.session_id!==target.id || result.title!==title || !Number.isInteger(result.title_version))throw Error('The saved name was not confirmed. Check history before retrying.');
    $('renameDialog').close();renameTarget=null;
    if(session===target.id){$('chatTitle').textContent=title;$('chatTitle').setAttribute('title',title);}
    try {if(session===target.id)await refresh();else await sessions({quiet:true});}
    catch {error('Name saved, but the view could not refresh. Use Refresh history to reload.');}
    recentButtons.get(target.id)?.focus();
  } catch(e) {
    $('renameError').textContent=e.message;$('renameError').hidden=false;
    if(e.code==='TITLE_CONFLICT')await sessions({quiet:true});
  } finally {renamingSession='';$('saveRename').disabled=false;$('cancelRename').disabled=false;}
}
$('renameMenuItem').onclick=askRename;$('renameForm').onsubmit=saveRename;
$('cancelRename').onclick=()=>{$('renameDialog').close();renameTarget=null;menuAnchor?.focus();};
$('renameDialog').oncancel=e=>{if(renamingSession)e.preventDefault();else renameTarget=null;};
window.addEventListener('pointerdown',e=>{if(!$('sessionMenu').contains(e.target))closeSessionMenu();});
window.addEventListener('keydown',e=>{
  if($('sessionMenu').hidden)return;
  if(['Escape','Tab'].includes(e.key)){if(e.key==='Escape')e.preventDefault();closeSessionMenu(true);}
  else if(['ArrowDown','ArrowUp','Home','End'].includes(e.key)){
    e.preventDefault();menuFocus=e.key==='Home'?0:e.key==='End'?1:1-menuFocus;
    $([ 'renameMenuItem','deleteMenuItem'][menuFocus]).focus();
  }
});
window.addEventListener('resize',()=>closeSessionMenu());
$('recentList').onscroll=()=>closeSessionMenu();
async function sessions({quiet=false}={}) {
  loadTransfers();
  const ticket=++recentIssued,query=$('sessionSearch').value.trim();
  $('recentList').setAttribute('aria-busy','true');
  try {
    const data=await api('/api/sessions'+(query ? '?q='+encodeURIComponent(query) : ''));
    if(ticket===recentIssued){
      if(query!==renderedRecentQuery){$('recentList').scrollTop=0;renderedRecentQuery=query;}
      renderRecents(data);
    }
    return data.sessions;
  } catch(e) {
    if(ticket===recentIssued)$('recentStatus').textContent='Could not load history. Previous results are kept. Use Refresh history to retry.';
    if(!quiet)throw e;return [];
  } finally {if(ticket===recentIssued)$('recentList').setAttribute('aria-busy','false');}
}
async function switchSession(id) {
  stopReadback();
  if(id===session && !switchingSession){await refresh();return;}
  const scope=++sessionEpoch;switchingSession=true;invalidateReview();updateComposer();updateReviewControls();
  try {
  // Resolve ownership/availability before clearing the old page or its unsent text.
  const state=await api(`/api/state?session=${encodeURIComponent(id)}`);
  if(scope!==sessionEpoch)return;
  if(state.session_id!==id)throw Error('Saved session did not match. Refresh history and try again.');
  await discard();if(scope!==sessionEpoch)return;
  $('draftDialog').close();$('receiptDialog').close();closeSessionMenu();
  if(session)editorDrafts.set(session,$('message').value);
  session=id;$('message').value=editorDrafts.get(id) || '';updateComposer();
  currentDraft=null;lastStateSequence=-1;reviewGate={ready:false};save('dcta-session',session);
  if(config?.payments_ready) { pendingPayment=null; try {pendingPayment=validPending(JSON.parse(localStorage.getItem(`dcta-payment-${session}`)||'null'),session);}catch{} }
  render(state);selectRecent();error();await sessions({quiet:true});
  if(scope!==sessionEpoch)return;
  if(config?.payments_ready) {pendingMessage('');if(pendingPayment) await checkPayment();}
  const last=state.voices.find(x=>['PROCESSING','READY'].includes(x.status));
  if(last) { voice={...last,session}; showVoice(last); if(last.status==='PROCESSING') poll(voiceEpoch); }
  } finally {if(scope===sessionEpoch){switchingSession=false;updateComposer();updateReviewControls();}}
}
async function send(text, fromComposer=false) {
  if(!text.trim() || sendingText || switchingSession || creatingSession) return;
  if(text.length>2000) {error('Please keep your message within 2,000 characters.');return;}
  stopReadback();
  if(!session){await newConversation();if(!session)return;}
  const sid=session,scope=sessionEpoch;
  sendingText=true;updateComposer();
  turnsInFlight++; $('connection').textContent='Working…';
  if(config?.payments_ready) $('reviewDraft').disabled=true;
  try {
    await discard();
    if(sid!==session || scope!==sessionEpoch)return;
    error();showWaiting(text);await api('/api/turn',{session:sid,text,key:crypto.randomUUID()});
    // Acknowledged text can be cleared, but never erase a newer edit or another session.
    if(fromComposer && editorDrafts.get(sid)===text)editorDrafts.delete(sid);
    if(sid===session && scope===sessionEpoch) {
      if(fromComposer && $('message').value===text)$('message').value='';
      speakNext=true;await refresh();speakNext=false;
    }
  }
  catch(e) { if(sid===session && scope===sessionEpoch){error(`${e.message} ${fromComposer?'Your text is kept. ':''}Check saved conversation before sending it again.`);await refresh().catch(()=>{});} }
  finally { hideWaiting();sendingText=false;turnsInFlight--; $('connection').textContent=turnsInFlight ? 'Working…' : 'Public demo';updateComposer(); updateReviewControls(); }
}
function hidePlayback() { $('playback').pause(); $('playback').removeAttribute('src'); $('playback').hidden=true; if(playbackUrl) URL.revokeObjectURL(playbackUrl); playbackUrl=''; $('listen').textContent='Listen';$('listen').disabled=false; }
function showVoice(item) {
  $('voicePanel').hidden=false;
  const ready=item.status==='READY';
  $('voiceLabel').textContent=ready ? 'Voice Review' : 'Speech recognition';
  $('voiceStatus').textContent=ready ? 'Check every detail. Keys: Alt+Enter use · Alt+E edit as text · Esc discard.' : item.status==='PROCESSING' ? (config?.asr_processing_message || 'Recognizing audio… You can discard this recording or continue with text.') : item.status==='CANCELLED'?'Recording cancelled.':item.status==='SUBMITTED'?'Transcript submitted.':issueText(item.error_code||'ASR_FAILED');
  $('transcript').textContent=item.transcript || '';
  $('useTranscript').hidden=!ready; $('editTranscript').hidden=!ready; $('listen').hidden=!item.id;
  $('useTranscript').disabled=false;
  $('discardVoice').disabled=false; $('closeVoice').disabled=false; $('editTranscript').disabled=false;
}
function releaseCapture(active) {
  clearTimeout(active.limit);clearInterval(active.tick);
  active.stream.getTracks().forEach(t=>t.stop());
  if(capture===active){capture=null;updateRecordLabel();}
}
// Keep recording briefly after Stop/release: people often release the button on their last word.
const STOP_TAIL_MS=450;
function stopCapture(accept, active=capture, tail=0) {
  if(!active)return;
  clearTimeout(active.tailTimer);
  if(accept && tail>0 && !active.finishing && active.recorder.state==='recording') {
    active.finishing=true;active.accept=true;
    if(active.epoch===voiceEpoch)$('voiceStatus').textContent='Finishing…';
    active.tailTimer=setTimeout(()=>stopCapture(true,active),tail);return;
  }
  active.accept=accept;
  try {if(active.recorder.state!=='inactive')active.recorder.stop();}
  catch {active.accept=false;if(active.epoch===voiceEpoch)error('Recording could not finish. Please record again or use text.');}
  finally {releaseCapture(active);}
}
async function discard() {
  invalidateReview();
  const epoch=++voiceEpoch;pendingCapture=false;clearTimeout(pollTimer);stopCapture(false);updateRecordLabel();
  const old=voice;hidePlayback();$('voicePanel').hidden=true;$('transcript').textContent='';
  if(old && ['PROCESSING','READY'].includes(old.status)) {
    let cancellation=voiceCancellations.get(old.id);
    if(!cancellation) {
      cancellation=api('/api/voice/cancel',{session:old.session,id:old.id}).finally(()=>voiceCancellations.delete(old.id));
      voiceCancellations.set(old.id,cancellation);
    }
    try {await cancellation;}
    catch(e) {if(epoch===voiceEpoch && voice===old){showVoice(old);error('Could not discard the recording. Try discarding it again before continuing.');}throw e;}
  }
  if(voice===old)voice=null;
}
async function poll(epoch) {
  if(!voice || epoch!==voiceEpoch) return;
  const item=voice;
  try {
    const result=await api(`/api/voice?session=${encodeURIComponent(item.session)}&id=${encodeURIComponent(item.id)}`);
    if(epoch!==voiceEpoch || voice?.id!==item.id) return;
    voice={...result,session:item.session}; showVoice(result);
    if(result.status==='PROCESSING') pollTimer=setTimeout(()=>poll(epoch),700);
  } catch(e) { if(epoch===voiceEpoch) { error(e.message); $('voiceStatus').textContent='Recognition status unavailable. Discard and use text, or reload to inspect it.'; } }
}
async function upload(blob,epoch,sid) {
  if(epoch!==voiceEpoch || sid!==session) return;
  if(!validAudioSize(blob)) { $('voicePanel').hidden=true; return; }
  showVoice({status:'PROCESSING'});
  try {
    const result=await api(`/api/audio?session=${encodeURIComponent(sid)}&key=${crypto.randomUUID()}`,blob,true);
    if(epoch!==voiceEpoch || sid!==session) { await api('/api/voice/cancel',{session:sid,id:result.id}); return; }
    voice={...result,session:sid}; showVoice(result); if(result.status==='PROCESSING') poll(epoch);
  } catch(e) { if(epoch===voiceEpoch) { error(e.message); $('voiceStatus').textContent='Upload response unavailable. Reload to check saved recordings before uploading again.'; } }
}
function validAudioSize(blob) {
  if(blob.size>=100 && blob.size<=6*1024*1024)return true;
  error('Recording is empty or exceeds 6 MB. Try a shorter recording or use text.');return false;
}
async function startRecording() {
  stopReadback();
  if(pendingCapture || capture || switchingSession || creatingSession || !config?.asr_ready) return;
  if(!session){await newConversation();if(!session)return;}
  if(!navigator.mediaDevices?.getUserMedia || !window.MediaRecorder) { error('Recording is not supported here. Open this local address in Chrome or Edge, upload audio, or use text.'); return; }
  const clearing=discard(); pendingCapture=true; const epoch=voiceEpoch, sid=session;
  let acquiredStream,active;
  try {
    updateRecordLabel();await clearing;
    if(epoch!==voiceEpoch || sid!==session)return;error();
    $('discardVoice').disabled=false; $('closeVoice').disabled=false;
    $('voicePanel').hidden=false; $('voiceLabel').textContent='Microphone'; $('voiceStatus').textContent='Waiting for microphone permission…'; $('transcript').textContent='';
    for(const id of ['useTranscript','editTranscript','listen']) $(id).hidden=true;
    const device=$('micSelect').value;
    // Preferences keep older microphones usable; only an explicitly selected device is required.
    const audio={echoCancellation:true,noiseSuppression:true,autoGainControl:true,channelCount:{ideal:1}};
    if(device)audio.deviceId={exact:device};
    const stream=await navigator.mediaDevices.getUserMedia({audio});
    acquiredStream=stream;
    if(epoch!==voiceEpoch || sid!==session) { stream.getTracks().forEach(t=>t.stop()); return; }
    const mime=['audio/webm;codecs=opus','audio/ogg;codecs=opus','audio/mp4'].find(x=>MediaRecorder.isTypeSupported(x));
    const recorder=new MediaRecorder(stream,mime ? {mimeType:mime} : {});
    active={recorder,stream,chunks:[],bytes:0,accept:true,epoch,sid,started:Date.now()};capture=active;
    recorder.ondataavailable=e=>{
      if(!active.accept || epoch!==voiceEpoch || sid!==session || !e.data.size)return;
      active.bytes+=e.data.size;
      if(active.bytes>6*1024*1024){active.chunks=[];stopCapture(false,active);error('Recording exceeds 6 MB. Try a shorter recording or use text.');$('voicePanel').hidden=true;return;}
      active.chunks.push(e.data);
    };
    recorder.onerror=()=>{
      stopCapture(false,active);
      if(epoch===voiceEpoch && sid===session){error('Microphone recording failed. Please use text or try again.');$('voicePanel').hidden=true;}
    };
    recorder.onstop=()=>{
      releaseCapture(active);
      if(active.accept && epoch===voiceEpoch && sid===session){
        active.accept=false;upload(new Blob(active.chunks,{type:recorder.mimeType}),epoch,sid);
      }
      active.chunks=[];
    };
    recorder.start(250);active.started=Date.now();updateRecordLabel();$('voiceLabel').textContent='Listening · English';
    const tick=()=>{if(capture===active && !active.finishing)$('voiceStatus').textContent=`● Speak now · ${Math.floor((Date.now()-active.started)/1000)} / 30 seconds · Stop after your last word.`;};tick();
    active.tick=setInterval(tick,250);active.limit=setTimeout(()=>stopCapture(true,active),29000);
  } catch(e) {
    if(active)stopCapture(false,active);else acquiredStream?.getTracks().forEach(t=>t.stop());
    if(epoch===voiceEpoch) {
      if(voice)error('Could not discard the previous recording. Discard it again before recording.');
      else {$('voicePanel').hidden=true;error(issueText(e.name==='NotAllowedError'?'MIC_DENIED':'MIC_UNAVAILABLE'));}
    }
  }
  finally {if(epoch===voiceEpoch){pendingCapture=false;updateRecordLabel();}}
}
function updateRecordLabel() {
  const recording=!!capture,stoppable=recording || pendingCapture;
  const label=recording ? 'Stop recording' : pendingCapture ? 'Cancel microphone request' : $('recordMode').value==='hold' ? 'Hold to talk' : 'Record voice';
  $('record').setAttribute('aria-label',label);$('record').title=`${label} (${recording||pendingCapture?'Alt+V to stop · Esc to discard':$('recordMode').value==='hold'?'hold Alt+V':'Alt+V'})`;
  $('record').setAttribute('aria-keyshortcuts','Alt+V');$('record').setAttribute('aria-pressed',String(stoppable));
  $('record').classList.toggle('recording',recording);$('recordIcon').src=stoppable ? '/icons/stop.svg' : '/icons/microphone.svg';
}
function updateComposer() {
  const input=$('message');input.style.height='auto';input.style.height=`${Math.min(180,Math.max(40,input.scrollHeight))}px`;
  $('send').disabled=!input.value.trim() || sendingText || switchingSession || creatingSession || !config;
  $('compose').setAttribute('aria-busy',String(sendingText || switchingSession));
  // Voice entry points stay usable with no conversation yet: using one starts a session
  // (same as sending text). Only transient switches and missing ASR setup block them.
  const busy=switchingSession || creatingSession;
  $('record').disabled=!config?.asr_ready || busy;
  $('uploadAudio').disabled=!config?.asr_ready || busy;
  $('audioFile').disabled=!config?.asr_ready || busy;
  $('newSession').disabled=creatingSession || switchingSession || !config;
}
$('compose').onsubmit=e=>{e.preventDefault();return send($('message').value,true);};
// Enter sends; Ctrl/Cmd+Enter (or Shift+Enter) inserts a new line. IME confirmation never sends.
$('message').onkeydown=e=>{
  if(e.key!=='Enter' || e.isComposing || e.keyCode===229 || e.altKey) return;
  if(e.shiftKey) return;  // native new line
  e.preventDefault();
  if(e.ctrlKey || e.metaKey){
    const box=$('message'),start=box.selectionStart??box.value.length,end=box.selectionEnd??start;
    if(box.setRangeText)box.setRangeText('\n',start,end,'end');else box.value=box.value.slice(0,start)+'\n'+box.value.slice(end);
    try{box.dispatchEvent(new Event('input',{bubbles:true}));}catch{box.oninput?.();}
    return;
  }
  $('compose').requestSubmit();
};
$('message').oninput=()=>{invalidateReview();updateComposer();};
$('record').onclick=()=>{ if($('recordMode').value==='click') { if(capture) stopCapture(true,capture,STOP_TAIL_MS); else if(pendingCapture) discard().catch(e=>error(e.message)); else startRecording().catch(e=>error(e.message)); }};
$('record').onpointerdown=e=>{ if($('recordMode').value==='hold') { e.preventDefault(); $('record').setPointerCapture(e.pointerId); startRecording().catch(e=>error(e.message)); }};
function releaseHold() { if($('recordMode').value==='hold') { if(capture) stopCapture(true,capture,STOP_TAIL_MS); else if(pendingCapture) discard().catch(e=>error(e.message)); } }
$('record').onpointerup=releaseHold; $('record').onpointercancel=()=>discard().catch(e=>error(e.message));
$('record').onkeydown=e=>{if($('recordMode').value==='hold' && (e.code==='Space'||e.code==='Enter')) { e.preventDefault(); if(!e.repeat) startRecording().catch(e=>error(e.message)); }};
$('record').onkeyup=e=>{if($('recordMode').value==='hold' && (e.code==='Space'||e.code==='Enter')) { e.preventDefault(); releaseHold(); }};
window.addEventListener('blur',()=>{if((capture || pendingCapture) && $('recordMode').value==='hold')discard().catch(e=>error(e.message));});
// Voice keyboard shortcuts (work anywhere on the page, including while typing in the message box):
//   Alt+V      start / stop recording (in "Hold to talk" mode: hold Alt+V, release to stop)
//   Esc        discard the recording or the transcript under review
//   Alt+Enter  use the reviewed transcript        Alt+E  edit the transcript as text
// Alt (Option on a Mac) avoids Ctrl+Space, which switches the input method on Windows.
function anyDialogOpen(){try{return [...document.querySelectorAll('dialog')].some(d=>d.open);}catch{return false;}}
function voiceKeys(e){
  if(e.ctrlKey || e.metaKey || e.isComposing)return;
  const alt=e.altKey;
  if(alt && e.code==='KeyV'){
    e.preventDefault();if(e.repeat || anyDialogOpen())return;
    if($('record').disabled && !capture && !pendingCapture)return;
    if($('recordMode').value==='hold'){if(!capture && !pendingCapture)startRecording().catch(x=>error(x.message));}
    else $('record').onclick();
    return;
  }
  if(anyDialogOpen())return;
  const reviewing=!$('voicePanel').hidden;
  if(alt && e.key==='Enter' && reviewing && !$('useTranscript').hidden && !$('useTranscript').disabled){e.preventDefault();$('useTranscript').onclick();return;}
  if(alt && e.code==='KeyE' && reviewing && !$('editTranscript').hidden && !$('editTranscript').disabled){e.preventDefault();$('editTranscript').onclick();return;}
  if(!alt && e.key==='Escape' && (capture || pendingCapture || (reviewing && !$('discardVoice').disabled))){e.preventDefault();dismissVoiceReview();}
}
function voiceKeysUp(e){
  if($('recordMode').value!=='hold' || !(capture || pendingCapture))return;
  if(e.code==='KeyV' || e.key==='Alt'){e.preventDefault();releaseHold();}
}
try{document.addEventListener?.('keydown',voiceKeys);document.addEventListener?.('keyup',voiceKeysUp);}catch{}
async function dismissVoiceReview() {
  if(voice?.status==='SUBMITTED')return;
  const sid=session,scope=sessionEpoch;
  try {
    await discard();
    if(sid===session && scope===sessionEpoch && !voice && !capture && !pendingCapture){updateComposer();$('message').focus();sessions({quiet:true});}
  } catch(e){error(e.message);}
}
$('discardVoice').onclick=dismissVoiceReview;
$('closeVoice').onclick=dismissVoiceReview;
$('useTranscript').onclick=async()=>{
  invalidateReview();
  if(!voice || voice.status!=='READY') return; const item=voice; const epoch=voiceEpoch;
  $('useTranscript').disabled=true; $('discardVoice').disabled=true; $('closeVoice').disabled=true; $('editTranscript').disabled=true; voice.status='SUBMITTED'; $('voiceStatus').textContent='Sending this final transcript… Send a new text message to supersede it.';
  try { showWaiting(item.transcript||'','VOICE');await api('/api/voice/submit',{session:item.session,id:item.id}); hideWaiting(); if(session===item.session) { speakNext=true;await refresh();speakNext=false; if(epoch===voiceEpoch) { voice=null; $('voicePanel').hidden=true; hidePlayback(); } } }
  catch(e) { hideWaiting(); error(e.message); if(epoch===voiceEpoch) { $('voiceStatus').textContent='Submission failed or is pending. Reload to inspect the saved result before retrying.'; } }
};
$('editTranscript').onclick=async()=>{
  const text=voice?.transcript,sid=session,scope=sessionEpoch,previous=$('message').value;
  const clearing=discard(),epoch=voiceEpoch;
  try {await clearing;if(epoch!==voiceEpoch || sid!==session || scope!==sessionEpoch || $('message').value!==previous)return;
    $('message').value=text || '';updateComposer();$('message').focus();
  } catch(e) {if(epoch===voiceEpoch)error(e.message);}
};
$('playback').onplay=()=>{$('listen').textContent='Pause';};
$('playback').onpause=$('playback').onended=()=>{$('listen').textContent='Listen';};
$('listen').onclick=async()=>{
  if(!voice || $('listen').disabled)return;
  const item=voice,epoch=voiceEpoch;$('listen').disabled=true;
  try {
    if(playbackUrl){if($('playback').paused)await $('playback').play();else $('playback').pause();return;}
    const response=await fetch(`/api/audio?session=${encodeURIComponent(item.session)}&id=${encodeURIComponent(item.id)}`,{headers:{'X-DCTA-Token':token}});
    if(!response.ok)throw new Error('Audio unavailable.');
    const blob=await response.blob();if(epoch!==voiceEpoch)return;
    hidePlayback();playbackUrl=URL.createObjectURL(blob);$('playback').src=playbackUrl;await $('playback').play();
  } catch(e){if(epoch===voiceEpoch)error(e.message);}
  finally{if(epoch===voiceEpoch)$('listen').disabled=false;}
};
$('uploadAudio').onclick=()=>$('audioFile').click();
$('audioFile').onchange=async()=>{
  const file=$('audioFile').files[0];$('audioFile').value='';
  if(!file || switchingSession || creatingSession || !config?.asr_ready || !validAudioSize(file))return;
  if(!session){await newConversation();if(!session)return;}
  const sid=session,scope=sessionEpoch,clearing=discard(),epoch=voiceEpoch;
  try {await clearing;if(epoch!==voiceEpoch || sid!==session || scope!==sessionEpoch)return;error();await upload(file,epoch,sid);}
  catch(e){if(epoch===voiceEpoch)error(e.message);}
};
$('cancelDraft').onclick=()=>{$('draftDialog').close();return send('Cancel it.').catch(e=>error(e.message));};
async function newConversation(){
  if(creatingSession || switchingSession || !config)return;
  const scope=sessionEpoch;creatingSession=true;updateComposer();
  try {const data=await api('/api/session',{});if(scope!==sessionEpoch){await sessions({quiet:true});return;}if(!session)editorDrafts.set(data.session_id,$('message').value);$('sessionSearch').value='';await switchSession(data.session_id);}
  catch(e){error(e.message);await sessions({quiet:true});}
  finally{creatingSession=false;updateComposer();}
}
$('newSession').onclick=newConversation;
$('settingsButton').onclick=()=>$('settings').showModal();
// A persistent desktop sidebar becomes a native modal drawer on small screens.
// Reusing the same nodes preserves search, selection and keyboard behavior.
const workspaceMedia=window.matchMedia?.('(max-width:720px)');
const workspaceCompactMedia=window.matchMedia?.('(max-width:1100px)');
let historyOpen=saved('dcta-history-open','true')!=='false';
let transfersOpen=saved('dcta-transfers-open','false')==='true';
function syncSidebarButtons(){
  const mobile=!!workspaceMedia?.matches;
  $('historyToggle').setAttribute('aria-expanded',String(mobile?!!$('historyDrawer').open:!$('historySlot').hidden));
  $('transfersButton').setAttribute('aria-expanded',String(mobile?!!$('transfersDrawer').open:!$('transfersSlot').hidden));
}
function syncWorkspaceLayout() {
  const mobile=!!workspaceMedia?.matches;
  closeSessionMenu();
  closeHistoryDrawer();
  closeTransfersDrawer();
  for(const [panel,slot,drawer] of [['recentSidebar','historySlot','historyDrawer'],['transfersSidebar','transfersSlot','transfersDrawer']]){
    const destination=$(mobile?drawer:slot);
    if($(panel).parentElement!==destination)destination.append($(panel));
  }
  // On smaller desktops, opening Transfers temporarily gives it the sidebar space.
  // Closing it restores the user's History preference.
  const historyVisible=!mobile && historyOpen && !(workspaceCompactMedia?.matches && transfersOpen);
  $('historySlot').hidden=!historyVisible;
  $('transfersSlot').hidden=mobile || !transfersOpen;
  $('workspace').classList.toggle('history-collapsed',!historyVisible);
  $('workspace').classList.toggle('transfers-open',!mobile && transfersOpen);
  $('historyToggle').setAttribute('aria-controls',mobile?'historyDrawer':'recentSidebar');
  $('transfersButton').setAttribute('aria-controls',mobile?'transfersDrawer':'transfersSidebar');
  for(const id of ['historyToggle','transfersButton'])$(id).setAttribute('aria-haspopup',mobile?'dialog':'false');
  syncSidebarButtons();
}
function closeHistoryDrawer() {
  if($('historyDrawer').open)$('historyDrawer').close();
  syncSidebarButtons();
}
function closeTransfersDrawer(){
  if($('transfersDrawer').open)$('transfersDrawer').close();
  syncSidebarButtons();
}
$('historyToggle').onclick=()=>{
  if(workspaceMedia?.matches){closeTransfersDrawer();if($('historyDrawer').open)closeHistoryDrawer();else $('historyDrawer').showModal();syncSidebarButtons();}
  else {
    if(workspaceCompactMedia?.matches && transfersOpen){transfersOpen=false;save('dcta-transfers-open','false');historyOpen=true;}
    else historyOpen=!historyOpen;
    save('dcta-history-open',String(historyOpen));syncWorkspaceLayout();
  }
};
$('closeHistory').onclick=closeHistoryDrawer;
$('historyDrawer').onclose=()=>{closeSessionMenu();syncSidebarButtons();};
$('transfersDrawer').onclose=syncSidebarButtons;
workspaceMedia?.addEventListener?.('change',syncWorkspaceLayout);
workspaceCompactMedia?.addEventListener?.('change',syncWorkspaceLayout);
syncWorkspaceLayout();
$('transfersButton').onclick=()=>{
  if(workspaceMedia?.matches){closeHistoryDrawer();if($('transfersDrawer').open)closeTransfersDrawer();else $('transfersDrawer').showModal();syncSidebarButtons();}
  else {transfersOpen=!transfersOpen;save('dcta-transfers-open',String(transfersOpen));syncWorkspaceLayout();}
};
$('closeTransfers').onclick=()=>{
  if(workspaceMedia?.matches)closeTransfersDrawer();
  else {transfersOpen=false;save('dcta-transfers-open','false');syncWorkspaceLayout();}
  $('transfersButton').focus();
};
$('historySearch').onsubmit=e=>{e.preventDefault();sessions({quiet:true});};
$('sessionSearch').oninput=()=>{if(!$('sessionSearch').value)sessions({quiet:true});};
$('refreshSessions').onclick=()=>session ? sessions({quiet:true}) : connectWorkspace();
$('recordMode').value=saved('dcta-record-mode')==='hold' ? 'hold' : 'click';
$('recordMode').onchange=()=>{save('dcta-record-mode',$('recordMode').value);if(capture || pendingCapture)discard().catch(e=>error(e.message));updateRecordLabel();};updateRecordLabel();
$('micSelect').onchange=()=>{if(capture || pendingCapture)discard().catch(e=>error(e.message));};
$('refreshMics').onclick=async()=>{let stream; try {stream=await navigator.mediaDevices.getUserMedia({audio:true}); const devices=await navigator.mediaDevices.enumerateDevices(); $('micSelect').replaceChildren(new Option('System default','')); for(const device of devices.filter(x=>x.kind==='audioinput')) $('micSelect').append(new Option(device.label || 'Microphone',device.deviceId));} catch {error('Microphone access unavailable. You can still use text or upload audio.');} finally{stream?.getTracks().forEach(t=>t.stop());} };
window.addEventListener('pagehide',()=>{stopReadback();voiceEpoch++;pendingCapture=false;stopCapture(false);hidePlayback();});

function invalidateReview() {
  stopReadback();
  if(!config?.payments_ready) return;
  reviewEpoch++; const old=review;review=null;reviewing=false;clearInterval(reviewTimer);
  if(currentDraft && /checked/i.test($('draftState').textContent)){$('draftState').textContent='Draft · not validated';setTone($('draftState'),'');}
  $('reviewStatus').textContent='Review runs an independent check before authorization.';
  $('reviewDialog').close(); $('signPayment').disabled=true;
  if(old) {
    invalidating=invalidating.catch(()=>{}).then(()=>api('/api/review/cancel',{session:old.session,challenge_id:old.id}));
    invalidating.catch(e=>error(e.message));
  }
}
function updateReviewControls() {
  updateSteps();
  if(!config?.payments_ready)return;
  $('reviewDraft').disabled=!currentDraft || !reviewGate.ready || changeToConfirm() || reviewing || signing || switchingSession || !!pendingPayment || turnsInFlight>0;
  $('signPayment').disabled=!review || signing || switchingSession || !$('reviewCheck').checked || Date.now()>=review.expires_ms;
}
function validPending(item,sid) {
  if(!item || typeof item!=='object' || item.session!==sid || !['session','challenge_id','key'].every(k=>typeof item[k]==='string' && item[k].length>0 && item[k].length<=100))return null;
  return {session:item.session,challenge_id:item.challenge_id,key:item.key};
}
function samePending(item) {return !!pendingPayment && item.session===session && item.session===pendingPayment.session && item.key===pendingPayment.key && item.challenge_id===pendingPayment.challenge_id;}
function clearPending(item) {
  if(!samePending(item))return false;
  pendingPayment=null;
  try {
    const name=`dcta-payment-${item.session}`,stored=JSON.parse(localStorage.getItem(name)||'null');
    if(stored?.key===item.key)localStorage.removeItem(name);
  } catch {} // Server receipt/status is authoritative even if storage cleanup fails.
  return true;
}
function b64(buffer) {return btoa(String.fromCharCode(...new Uint8Array(buffer)));}
// Device biometrics (WebAuthn platform authenticator: Windows Hello / Touch ID) when opened on localhost.
function b64url(buffer){return b64(buffer).replace(/\+/g,'-').replace(/\//g,'_').replace(/=+$/,'');}
function fromB64url(text){const s=atob(text.replace(/-/g,'+').replace(/_/g,'/')+'='.repeat((4-text.length%4)%4));return Uint8Array.from(s,c=>c.charCodeAt(0));}
async function deviceBiometricAvailable(){
  try{return window.isSecureContext && !!config?.webauthn_rp_id && !!window.PublicKeyCredential && await window.PublicKeyCredential.isUserVerifyingPlatformAuthenticatorAvailable();}catch{return false;}
}
function rememberedDeviceKey(){try{const value=JSON.parse(localStorage.getItem('dcta.deviceKey')||'null');return value&&value.id&&value.credentialId?value:null;}catch{return null;}}
async function getDeviceKey(){
  const saved=rememberedDeviceKey();
  if(saved){try{if((await api('/api/webauthn/known',{key_id:saved.id})).known)return {...saved,kind:'webauthn'};}catch{}}
  const begin=await api('/api/webauthn/register/begin',{purpose:'authorization'});
  $('reviewStatus').textContent='Set up device verification: follow the Windows Hello / Touch ID prompt.';
  const credential=await navigator.credentials.create({publicKey:{challenge:fromB64url(begin.challenge),rp:{id:begin.rp_id,name:'DCTA mock banking demo'},
    user:{id:fromB64url(begin.user_handle),name:'demo-user',displayName:'DCTA demo user'},pubKeyCredParams:[{type:'public-key',alg:-7}],
    authenticatorSelection:{authenticatorAttachment:'platform',userVerification:'required',residentKey:'discouraged'},attestation:'none',timeout:60000}});
  const response=credential.response;
  if(!response.getPublicKey || !response.getAuthenticatorData)throw Error('This browser cannot export the device key. Use a current Chrome or Edge.');
  const registered=await api('/api/webauthn/register/finish',{registration:begin.id,credential_id:b64url(credential.rawId),spki:b64(response.getPublicKey()),
    client_data:b64url(response.clientDataJSON),authenticator_data:b64url(response.getAuthenticatorData())});
  const key={id:registered.key_id,credentialId:registered.credential_id,kind:'webauthn'};
  try{localStorage.setItem('dcta.deviceKey',JSON.stringify({id:key.id,credentialId:key.credentialId}));}catch{}
  return key;
}
async function deviceAssertion(key,signingText){
  // The authenticator signs SHA-256 of the exact frozen payload, after local biometric/PIN verification.
  const challenge=await crypto.subtle.digest('SHA-256',new TextEncoder().encode(signingText));
  const credential=await navigator.credentials.get({publicKey:{challenge,rpId:config.webauthn_rp_id,allowCredentials:[{type:'public-key',id:fromB64url(key.credentialId)}],userVerification:'required',timeout:60000}});
  const r=credential.response;
  return {credential_id:b64url(credential.rawId),authenticator_data:b64url(r.authenticatorData),client_data:b64url(r.clientDataJSON),signature:b64url(r.signature)};
}
async function getDemoKey() {
  if(!demoKey && await deviceBiometricAvailable()) {
    try {demoKey=await getDeviceKey();}
    catch(e){error(issueText(typeof e.code==='string'?e.code:e.name==='NotAllowedError'?'DEVICE_CANCELLED':'DEVICE_UNAVAILABLE'));}
  }
  if(!crypto.subtle) throw Error('This browser cannot sign locally. Use Chrome or Edge on the local address.');
  if(!demoKey) {
    const pair=await crypto.subtle.generateKey({name:'ECDSA',namedCurve:'P-256'},false,['sign','verify']);
    const spki=b64(await crypto.subtle.exportKey('spki',pair.publicKey));
    const registered=await api('/api/demo-key',{spki});
    demoKey={privateKey:pair.privateKey,id:registered.key_id};
  }
  return demoKey;
}
function transactionReference(executionId) {
  if(typeof executionId!=='string' || !/^execution_[0-9a-f]{32}$/.test(executionId))return '';
  const alphabet='0123456789ABCDEFGHJKMNPQRSTVWXYZ';
  let value=BigInt('0x'+executionId.slice(10)),encoded='';
  for(let i=0;i<26;i++){encoded=alphabet[Number(value & 31n)]+encoded;value >>= 5n;}
  return 'DCTA-'+[encoded.slice(0,6),encoded.slice(6,12),encoded.slice(12,18),encoded.slice(18)].join('-');
}
function showReference(prefix,executionId) {
  const reference=transactionReference(executionId),button=$(prefix+'CopyReference'),status=$(prefix+'ReferenceStatus');
  $(prefix+'ReferenceLabel').hidden=!reference;$(prefix+'ReferenceRow').hidden=!reference;
  $(prefix+'Reference').textContent=reference;status.textContent='';button.disabled=false;
  button.setAttribute('aria-label','Copy reference number');
  button.onclick=async()=>{
    if(!reference || button.disabled)return;
    button.disabled=true;
    try {
      await navigator.clipboard.writeText(reference);
      if($(prefix+'Reference').textContent===reference){status.textContent='Copied';button.setAttribute('aria-label','Reference number copied');}
    } catch {
      if($(prefix+'Reference').textContent===reference)status.textContent='Could not copy. Select the number to copy it.';
    } finally {if($(prefix+'Reference').textContent===reference)button.disabled=false;}
  };
}
function showReceipt(receipt) {
  $('viewReceipt').hidden=!receipt;
  $('receiptPanel').hidden=!receipt;
  showReference('receipt',receipt?.execution_id);
  if(!receipt){$('receiptDialog').close();return;}
  // The reference is a lossless display form of the saved execution UUID.
  const st=lastRenderedState||{},info={purposes:receipt.purposes||st.receipt_purposes||[receipt.purpose!==undefined?receipt.purpose:st.receipt_purpose],
    defaults:receipt.purpose_defaults||st.receipt_purpose_defaults||['Transfer'],kinds:receipt.kinds||st.receipt_kinds||['PERSON']};
  const acts=receipt.actions&&receipt.actions.length?receipt.actions:[{amount_minor:receipt.amount_minor,payee:receipt.payee,source:receipt.source}];
  const many=renderParts('receiptActions',acts,info);$('receiptPlanNote').hidden=!many;$('receiptPlanNote').textContent=many?'Approved once and paid together: all or nothing.':'';
  setAmount($('receiptAmount'),acts.reduce((sum,a)=>sum+a.amount_minor,0));
  $('receiptHeadline').textContent=many?`${acts.length} payments`:receipt.payee.name;
  $('receiptPayee').textContent=many?`${acts.length} recipients (listed above)`:`${receipt.payee.name} · ending ${receipt.payee.ending}`;
  $('receiptSource').textContent=receipt.source?.name||acts[0].source?.name||'';
  $('receiptType').textContent=many?`Combined payment · ${acts.length} parts`:TYPE_LABEL[info.kinds[0]]||TYPE_LABEL.PERSON;
  const cat=categoryOf(info.purposes,info.defaults,0);setIcon($('receiptIcon'),many?'Transfer':cat,many?'PERSON':info.kinds[0]);heroIcons('receipt',info,acts.length);setIcon($('receiptCatIcon'),many?'Transfer':cat,many?'PERSON':info.kinds[0]);
  $('receiptDate').textContent=formatWhen(receipt.created_ms);
  if(many)categoryLines('receiptCatIcon','receiptPurpose',info,acts.length);else categorySingle('receiptCatIcon','receiptPurpose',purposeLabel(info.purposes[0],cat));
}
async function beginReview() {
  if(!currentDraft || !reviewGate.ready || reviewing || signing || switchingSession || pendingPayment || turnsInFlight) return;
  if(changeToConfirm()) {error('The amount or payee changed. Confirm the new details in the conversation first.');return;}
  if(purposeUnsaved()) {error($('draftPurpose').value==='Others'?'Write a short reason for "Others" and press Enter before reviewing.':'The purpose is still being saved. Try again in a moment.');$('draftDialog').showModal?.();$('draftPurposeNote').focus?.();return;}
  if(capture || pendingCapture || voice) {error('Use or discard the recording before review.');return;}
  if($('message').value.trim()) {error('Send or clear your unsent message before reviewing.');return;}
  invalidateReview();
  const epoch=reviewEpoch,sid=session,draft=currentDraft;reviewing=true;updateSteps();showReviewLoading(true);
  $('reviewDraft').disabled=true; $('reviewStatus').textContent='Checking your original instructions independently…'; error();
  try {
    await invalidating;
    if(epoch!==reviewEpoch || sid!==session)return;
    const key=await getDemoKey();
    const validation=await api('/api/validate',{session:sid,draft_id:draft.payload.draft_id,version:draft.current_version,content_hash:draft.content_hash});
    if(epoch!==reviewEpoch || sid!==session) return;
    $('reviewStatus').textContent=validation.decision==='PASS'?'Independent check passed.':issueText(validationIssue(validation));
    if(validation.decision!=='PASS') {
      const code=validationIssue(validation);
      error(issueText(code));showCheckResult(code);
      return;
    }
    const challenge=await api('/api/challenge',{session:sid,validation_id:validation.id,key_id:key.id});
    if(epoch!==reviewEpoch || sid!==session) {await api('/api/review/cancel',{session:sid,challenge_id:challenge.id});return;}
    review={...challenge,session:sid,epoch}; const r=challenge.review;
    $('reviewAmount').textContent=money(r.amount_minor);
    $('reviewSource').textContent=`${r.source.name} · ${r.source.id}`;
    $('reviewPayee').textContent=`${r.payee.name} · ending ${r.payee.ending}`;
    $('reviewFee').textContent=money(r.fee_minor);$('reviewPurpose').textContent=purposeSummary(currentPurposes.length?currentPurposes:[currentPurpose],purposeDefaults,'\n'); $('reviewTotal').textContent=money(r.total_minor);
    const plan=renderPlan('reviewActions','reviewPlanNote',r.actions||[],r.constraints||[],planDescribe);
    if(plan && (r.actions||[]).length>1){$('reviewPayee').textContent=`${r.actions.length} saved payees (listed above)`;$('reviewTitle').textContent=`Review ${r.actions.length} mock transfers`;}
    else $('reviewTitle').textContent='Review mock transfer';
    $('signPayment').textContent=key.kind==='webauthn'?'Verify with device & execute':'Sign & execute mock transfer';
    $('reviewCheckText').textContent=plan?'I checked every recipient, amount and source, and any minimum balance.':'I checked the recipient, amount and source.';

    $('reviewExpiry').textContent=`Valid until ${formatWhen(challenge.expires_ms).split(' · ')[1]}. Changes require a new review.`;
    $('draftState').textContent='Independently checked · waiting for your approval';setTone($('draftState'),$('draftState').textContent);$('reviewCheck').checked=false; $('signPayment').disabled=true; showReviewLoading(false); $('reviewDialog').showModal();speak(spokenReview(r));
    reviewTimer=setInterval(()=>{updateReviewControls();if(review && Date.now()>=review.expires_ms)$('reviewExpiry').textContent='Review expired. Close this page and review the draft again.';},500);
  } catch(e) {if(epoch!==reviewEpoch || sid!==session)return;const code=e.code||'CHECK_ERROR';error(issueText(code));$('reviewStatus').textContent=issueText(code);showCheckResult(code);}
  finally {if(!checkFailed)showReviewLoading(false);if(epoch===reviewEpoch) reviewing=false;updateReviewControls();}
}
// Blocking "loading" layer while the independent check runs: nothing can be clicked until the review opens or fails.
let checkFailed=false;
function resetCheckSurface(){
  checkFailed=false;$('reviewLoading').className='loading-dialog';$('checkAnim').hidden=false;$('checkResultMark').hidden=true;$('checkResultActions').hidden=true;
  $('reviewLoadingText').classList.remove('issue-description');
  $('reviewLoadingTitle').textContent='Independent check in progress';
  $('reviewLoadingText').textContent='A second AI is re-reading your original words and comparing them with the draft. Nothing has been sent.';
}
// The check did not pass: the same surface stays open and says why, instead of disappearing.
function showCheckResult(code){
  const layer=$('reviewLoading');if(!layer?.open)return;
  checkFailed=true;layer.className='loading-dialog failed';$('checkAnim').hidden=true;$('checkResultMark').hidden=false;$('checkResultActions').hidden=false;
  $('reviewLoadingTitle').textContent='Check not completed';
  $('reviewLoadingText').classList.add('issue-description');$('reviewLoadingText').textContent=issueText(code);
  $('checkRetry').focus?.();
}
function closeCheckSurface(){checkFailed=false;showReviewLoading(false);}
$('checkClose').onclick=()=>closeCheckSurface();
$('checkRetry').onclick=()=>{closeCheckSurface();beginReview();};
function showReviewLoading(on) {
  const layer=$('reviewLoading');if(!layer)return;
  if(on)resetCheckSurface();
  try {if(on){if(!layer.open)layer.showModal();}else if(layer.open)layer.close();}catch{}
}
$('reviewLoading').oncancel=e=>{e.preventDefault?.();if(checkFailed)closeCheckSurface();};
$('draftPurpose').onchange=()=>{$('draftPurposeStatus').textContent='';$('draftPurposeNote').hidden=$('draftPurpose').value!=='Others';if($('draftPurpose').value==='Others'){$('draftPurposeNote').focus();if(!$('draftPurposeNote').value.trim())return;}savePurpose();};
$('draftPurposeNote').onkeydown=e=>{if(e.key==='Enter'){e.preventDefault();savePurpose();}};
$('draftPurposeNote').onchange=()=>savePurpose();
function savePending(value) {
  // If persistence is unavailable, do not begin execution: recovery must be possible.
  const name=`dcta-payment-${session}`;
  if(value) localStorage.setItem(name,JSON.stringify(value)); else localStorage.removeItem(name);
  pendingPayment=value;
}
function pendingMessage(text) {
  $('pendingPanel').hidden=!pendingPayment;
  $('pendingStatus').textContent=text;
  $('retryPayment').disabled=!pendingPayment || signing;
  updateReviewControls();
}
async function submitOriginal() {
  const item=pendingPayment; if(!item) return;
  try {
    const result=await api('/api/execute',item);
    if(!samePending(item)) return;
    clearPending(item);pendingMessage('');showReceipt(result.receipt);celebrate(result.receipt);await refresh();
    $('reviewStatus').textContent='Mock transfer settled. Receipt saved.';
  } catch(e) {
    if(samePending(item)) {error(e.message);pendingMessage('Response unavailable or request rejected. Check the original request before doing anything else.');await checkPayment();}
  }
}
async function checkPayment() {
  const item=pendingPayment;if(!item)return;
  pendingMessage('Checking the original request…');
  try {
    const result=await api(`/api/execution?session=${encodeURIComponent(item.session)}&key=${encodeURIComponent(item.key)}`);
    if(!samePending(item))return;
    if(result.status==='SUCCEEDED') {clearPending(item);pendingMessage('');await refresh();showReceipt(result.receipt);}
    else if(['REJECTED','REVIEW_REQUIRED'].includes(result.status)) {clearPending(item);pendingMessage('');error(issueText(result.error_code));await refresh();}
    else pendingMessage(`${result.status}: no completed receipt yet. Check again, or explicitly retry the same request. A new transfer will remain blocked.`);
  } catch(e) {if(samePending(item)){pendingMessage('Could not check the saved request. Keep this page and check again.');error(e.message);}}
}
async function signAndExecute() {
  if(signing || switchingSession || !review || !$('reviewCheck').checked || review.epoch!==reviewEpoch) return;
  const item=review;signing=true;$('signPayment').disabled=true;error();let authorized=null;
  try {
    if(Date.now()>=item.expires_ms) throw issueError('EXPIRED');
    if(demoKey.kind==='webauthn'){
      const assertion=await deviceAssertion(demoKey,item.signing_text);
      if(item!==review || item.epoch!==reviewEpoch) return;
      authorized=await api('/api/authorize/webauthn',{session:item.session,challenge_id:item.id,key_id:demoKey.id,assertion});
    } else {
      const signature=b64(await crypto.subtle.sign({name:'ECDSA',hash:'SHA-256'},demoKey.privateKey,new TextEncoder().encode(item.signing_text)));
      if(item!==review || item.epoch!==reviewEpoch) return;
      authorized=await api('/api/authorize',{session:item.session,challenge_id:item.id,key_id:demoKey.id,signature});
    }
    if(item!==review || item.epoch!==reviewEpoch) return;
    if(authorized?.stepup_required || item.stepup_required) {
      // CV14 A: from SGD 800 the money only moves after the out-of-band code is verified by the host.
      const passed=await runStepUp(item);
      if(!passed || item!==review || item.epoch!==reviewEpoch) return;
    }
    savePending({session:item.session,challenge_id:item.id,key:crypto.randomUUID()});
    stopReadback();review=null;clearInterval(reviewTimer);$('reviewDialog').close();pendingMessage('Submitting the signed mock transfer…');
    await submitOriginal();
  } catch(e){error(e.message);}
  finally {signing=false;$('retryPayment').disabled=!pendingPayment;updateReviewControls();}
}
// --- CV14 A: extra security check (mock out-of-band token) -------------------------------------
function openDialog(id){try{if(!$(id).open)$(id).showModal();}catch{}}
function closeDialog(id){try{if($(id).open)$(id).close();}catch{}}
function stepupStatus(text,problem=false){$('stepupStatus').textContent=problem?`⚠ ${text}`:text;$('stepupStatus').className=problem?'small stepup-error':'small';}
// One live code at a time: "Send a new code" stays grey with a countdown until the current code runs out.
function stepupTimers(){clearInterval(stepup?.resendTimer);clearInterval(stepup?.waitTimer);}
function codeReady(){return /^[0-9]{6}$/.test(($('stepupCode').value||'').replace(/\s+/g,''));}
function updateStepUpButtons(){
  const current=stepup,left=current?.resendUntil?Math.max(0,Math.ceil((current.resendUntil-Date.now())/1000)):0;
  $('stepupSubmit').disabled=!current || current.busy || !current.check || !codeReady();
  $('stepupNewCode').disabled=!current || left>0 || current.busy;
  $('stepupNewCode').textContent=left>0?`Send a new code (${left})`:'Send a new code';
}
function showStepUpCode(check){
  const current=stepup;if(!current)return;
  clearInterval(current.waitTimer);$('stepupWait').hidden=true;$('stepupCodeArea').hidden=false;
  current.check=check;$('stepupCode').value='';
  const remaining=Number.isFinite(check.remaining_ms)?check.remaining_ms:Math.max(0,check.expires_ms-Date.now());
  current.resendUntil=Date.now()+remaining;
  stepupStatus(`A new code is on your security token. It expires in ${Math.max(1,Math.round(remaining/1000))} s · ${check.attempts_left} attempts.`);
  clearInterval(current.resendTimer);updateStepUpButtons();
  current.resendTimer=setInterval(()=>{if(stepup!==current){clearInterval(current.resendTimer);return;}updateStepUpButtons();if(Date.now()>=current.resendUntil)clearInterval(current.resendTimer);},250);
  $('stepupCode').focus?.();
}
function showStepUpWait(result){
  // A previous approval was stopped while its code was still live: wait, then continue automatically.
  const current=stepup;if(!current)return;
  clearInterval(current.resendTimer);$('stepupCodeArea').hidden=true;$('stepupWait').hidden=false;
  const until=Date.now()+result.wait_ms,total=result.wait_ms;
  const tick=()=>{if(stepup!==current){clearInterval(current.waitTimer);return;}
    const left=Math.max(0,until-Date.now());
    $('stepupWaitText').textContent=`Last action has been terminated, please wait ${Math.ceil(left/1000)}\u00a0s.`;
    $('stepupWaitBar').style.width=`${Math.min(100,left/total*100)}%`;
    if(left<=0){clearInterval(current.waitTimer);$('stepupWaitText').textContent='Getting your code…';issueStepUp(current.item).catch(e=>{showStepUpCodeError(e);});}};
  current.waitTick=tick;clearInterval(current.waitTimer);tick();current.waitTimer=setInterval(tick,250);
  $('stepupWaitCancel').focus?.();
}
function showStepUpCodeError(e){if(!stepup)return;$('stepupWait').hidden=true;$('stepupCodeArea').hidden=false;stepupStatus(e.message,true);updateStepUpButtons();}
async function issueStepUp(item){
  const result=await api('/api/stepup/issue',{session:item.session,challenge_id:item.id});
  if(!stepup || stepup.item!==item)return result;
  if(result.status==='WAIT')showStepUpWait(result);else showStepUpCode(result);
  return result;
}
function runStepUp(item){
  return new Promise(resolve=>{
    stepup={item,check:null,resolve,busy:false,resendUntil:0,resendTimer:null,waitTimer:null};
    $('stepupAmount').textContent=money(item.review.total_minor);
    $('stepupWait').hidden=true;$('stepupCodeArea').hidden=false;
    stepupStatus('Preparing your code…');$('stepupCode').value='';updateStepUpButtons();
    openDialog('stepupDialog');updateSteps();
    issueStepUp(item).catch(e=>showStepUpCodeError(e));
  });
}
function finishStepUp(passed){
  const current=stepup;stepupTimers();stepup=null;closeDialog('stepupDialog');updateSteps();
  if(current)current.resolve(passed);
}
async function submitStepUp(){
  const current=stepup;if(!current || current.busy)return;
  const code=($('stepupCode').value||'').replace(/\s+/g,'');
  if(!/^[0-9]{6}$/.test(code)){stepupStatus(issueText('STEPUP_CODE'),true);$('stepupCode').focus?.();return;}
  if(!current.check){stepupStatus(issueText('STEPUP_STATE'),true);return;}
  current.busy=true;updateStepUpButtons();
  try{
    const result=await api('/api/stepup/verify',{session:current.item.session,stepup_id:current.check.id,code});
    if(stepup!==current)return;
    if(result.status==='VERIFIED'){stepupStatus('Code accepted.');finishStepUp(true);return;}
    if(result.status==='FAILED'){finishStepUp(false);invalidateReview();error(issueText('SECURITY_PAUSE'));securityPause(result.pause_ms||10000);return;}
    current.check={...current.check,...result};$('stepupCode').value='';updateStepUpButtons();
    stepupStatus(issueText(result.status==='EXPIRED'?'STEPUP_EXPIRED':'STEPUP_CODE'),true);$('stepupCode').focus?.();
  }catch(e){if(stepup===current){if(e.code==='SECURITY_PAUSE'){finishStepUp(false);invalidateReview();securityPause(10000);}stepupStatus(e.message,true);}}
  finally{current.busy=false;if(stepup===current)updateStepUpButtons();}
}
// Settled: a drawn green tick, then "Success!". Purely visual; the receipt is already saved.
function celebrate(receipt){
  if(!receipt)return;
  const acts=receipt.actions&&receipt.actions.length?receipt.actions:[{amount_minor:receipt.amount_minor,payee:receipt.payee}];
  const total=acts.reduce((sum,a)=>sum+(a.amount_minor||0),0)||receipt.amount_minor;
  $('successDetail').textContent=`${money(total)} settled${acts.length===1&&acts[0].payee?.name?` to ${acts[0].payee.name}`:` in ${acts.length} transfers`}.`;
  const box=$('successDialog');box.classList?.remove('play');void box.offsetWidth;box.classList?.add('play');
  openDialog('successDialog');$('successClose').focus?.();
  clearTimeout(successTimer);successTimer=setTimeout(()=>closeSuccess(false),3200);
}
function closeSuccess(openReceipt){clearTimeout(successTimer);closeDialog('successDialog');if(openReceipt)$('viewReceipt').onclick?.();}
$('successClose').onclick=()=>closeSuccess(true);
$('successDialog').onclick=e=>{if(e.target===$('successDialog'))closeSuccess(false);};
$('successDialog').oncancel=e=>{e.preventDefault?.();closeSuccess(false);};
// After three wrong codes the server pauses every money action; the screen shows the same pause.
function securityPause(ms){
  if(!(ms>0))return;
  pauseUntil=Math.max(pauseUntil,Date.now()+ms);
  openDialog('pauseDialog');
  const tick=()=>{const left=Math.max(0,pauseUntil-Date.now());
    $('pauseSeconds').textContent=String(Math.ceil(left/1000));$('pauseBar').style.width=`${Math.min(100,left/ms*100)}%`;
    $('pauseStatus').textContent=left>0?issueText('SECURITY_PAUSE'):'You can continue now.';
    if(left<=0){clearInterval(pauseTimer);pauseTimer=null;closeDialog('pauseDialog');$('message')?.focus?.();}};
  clearInterval(pauseTimer);tick();pauseTimer=setInterval(tick,250);
}
$('pauseDialog').oncancel=e=>e.preventDefault?.();
$('stepupOpenToken').onclick=()=>{try{window.open('/token','dcta-security-token','width=460,height=680');}catch{}$('stepupCode').focus?.();};
$('stepupSubmit').onclick=()=>submitStepUp();
$('stepupCode').onkeydown=e=>{if(e.key==='Enter'){e.preventDefault();if(codeReady())submitStepUp();}};
$('stepupCode').oninput=()=>updateStepUpButtons();
$('stepupNewCode').onclick=()=>{if(stepup && !$('stepupNewCode').disabled)issueStepUp(stepup.item).catch(e=>stepupStatus(e.message,true));};
$('stepupCancel').onclick=()=>{finishStepUp(false);invalidateReview();$('reviewStatus').textContent='Transfer cancelled at the security check. No money moved.';};
$('stepupWaitCancel').onclick=()=>$('stepupCancel').onclick();
$('stepupDialog').oncancel=e=>{e.preventDefault?.();$('stepupCancel').onclick();};
$('viewDraft').onclick=()=>{if(currentDraft && !switchingSession)$('draftDialog').showModal();};
$('closeDraft').onclick=()=>$('draftDialog').close();
$('viewReceipt').onclick=()=>{if(!$('receiptPanel').hidden && !switchingSession)$('receiptDialog').showModal();};
$('closeReceipt').onclick=()=>$('receiptDialog').close();
$('reviewDraft').onclick=()=>{$('draftDialog').close();return beginReview().catch(e=>error(e.message));};
$('closeReview').onclick=()=>invalidateReview();
$('reviewDialog').oncancel=e=>{e.preventDefault();invalidateReview();};
$('reviewCheck').onchange=()=>updateReviewControls();
$('signPayment').onclick=()=>signAndExecute();
$('readAloud').value=readAloudOn()?'on':'off';
$('themeSelect').value=(()=>{try{return THEMES.includes(localStorage.getItem('dcta.theme'))?localStorage.getItem('dcta.theme'):'light';}catch{return 'light';}})();
$('themeSelect').onchange=()=>{const next=applyTheme($('themeSelect').value);save('dcta.theme',next);syncTheme();};
// The palette is the visible control; the (hidden) select stays the single source of truth.
for(const name of THEMES){const swatch=$(THEME_ID[name]);if(!swatch)continue;
  swatch.onclick=()=>{$('themeSelect').value=name;$('themeSelect').onchange();};}
syncTheme();
for(const name of ACCENTS){const swatch=$(accentId(name));if(!swatch)continue;swatch.onclick=()=>{applyAccent(name);save('dcta.accent',accent);syncAccent();};}
syncAccent();
$('readAloud').onchange=()=>{try{localStorage.setItem('dcta.readAloud',$('readAloud').value);}catch{}if($('readAloud').value!=='on')stopReadback()};
$('checkPayment').onclick=()=>checkPayment();
$('retryPayment').onclick=async()=>{if(signing || !pendingPayment)return;signing=true;pendingMessage('Retrying only the saved request…');try{await submitOriginal();}finally{signing=false;$('retryPayment').disabled=!pendingPayment;}};
function refreshFromServer(){refresh().catch(e=>error(e.message));loadTransfers();if(savedTransferRequest && !switchingSession){const {kind,id,version,compare,part}=savedTransferRequest;openSavedTransfer(kind,id,version,compare,part??null);}}
window.addEventListener('focus',refreshFromServer);
window.addEventListener('storage',event=>{if(event.key==='dcta-change' || event.key===`dcta-payment-${session}`)refreshFromServer();});
async function connectWorkspace(){
  if(initializing)return;initializing=true;
  try {
  config=await api('/api/config');
  const version=typeof config.version==='string' ? config.version : 'Unavailable';
  // Settings shows only what a customer needs. Provider, validator and speech-engine details
  // live in the README instead of the dialog.
  $('versionInfo').textContent=PUBLIC_VERSION; $('appVersion').textContent=`Version ${version.split('-')[0]}`; $('connection').textContent='Public demo';
  // Report the selected provider; a cloud mode must never claim local recognition.
  $('asrSetupNote').hidden=!!config.asr_ready;
  $('asrSetupNote').textContent=config.asr_provider==='trtc' ? 'Tencent speech recognition is not configured. Check the server’s TRTC credentials and dependencies, or type your request.' : 'Local speech recognition is not set up. Run Setup-Voice.ps1, or type your request.';
  $('speechRecognitionInfo').textContent=config.asr || 'Speech recognition';
  $('speechSynthesisInfo').textContent=config.tts || 'Browser voice · on device';
  $('readAloudHelp').textContent=config.tts_provider==='trtc'?'Uses Tencent TRTC cloud voice. When enabled, reply and review text is sent to Tencent for speech synthesis and may use paid quota. Check the details on screen before approving.':'Uses this browser’s built-in voice on this device. Check the details on screen before approving.';
  updateComposer();
  $('reviewDraft').hidden=!config.payments_ready;
  $('reviewStatus').textContent=config.payments_ready ? 'Review runs an independent check before authorization.' : 'Payment review needs DeepSeek and the payment dependencies. Offline mode supports drafts only.';
  $('sessionSearch').value='';const rows=await sessions();
  const stored=saved('dcta-session'),previous=/^[A-Za-z0-9_-]{1,100}$/.test(stored) ? stored : '';
  if(previous) {
    try {await switchSession(previous);return;}
    catch(e){if(!['NOT_FOUND','INPUT','HISTORY_REMOVED'].includes(e.code))throw e;save('dcta-session','');}
  }
  const id=rows[0]?.id;if(id)await switchSession(id);else clearWorkspace();
  if(previous)$('recentStatus').textContent='The previous session is unavailable. Opened a saved conversation instead.';
  } catch(e){error(e.message);$('connection').textContent='Connection unavailable';}
  finally{initializing=false;updateComposer();}
}
// Saved records are independent of the active editor and payment review.
let transferOffset=0,transferIssued=0,transferHasMore=false;
let savedTransfer=null,savedTransferRequest=null,savedTransferIssued=0;
// Status colours: neutral while a draft is waiting, green when checked or completed, red when cancelled or failed,
// amber when blocked or needing attention. The words always carry the meaning; colour only reinforces it.
// Transfer purpose: a label chosen in the draft window (Rent, Shopping… or Others with a short reason).
// Record-keeping only: it never changes the amount, payee, signed details or risk rules.
let currentPurpose=null,purposeDraftId='';
function purposeLabel(p,fallback='Transfer'){return !p?fallback:p.category==='Others'?`Others · ${p.note}`:p.category;}
// CV14: one purpose per action. Unpicked actions show their default (DBS shares → Investment, bills → Bills & utilities).
let currentPurposes=[],purposeDefaults=[],purposeRows=[];
function purposeSummary(list,defaults,sep=' · '){
  const n=Math.max(list?.length||0,defaults?.length||0,1);
  if(n===1)return purposeLabel(list?.[0],defaults?.[0]||'Transfer');
  return Array.from({length:n},(_,i)=>`${i+1}. ${purposeLabel(list?.[i],defaults?.[i]||'Transfer')}`).join(sep);
}
function purposeWorthShowing(list,defaults){return (list||[]).some(Boolean)||(defaults||[]).some(d=>d&&d!=='Transfer');}
function fillPurposeOptions(){
  // The page ships with the full list; the server's list (if any) only replaces it when it differs.
  const select=$('draftPurpose'),list=Array.isArray(config?.purposes)?config.purposes:[];
  if(!select || !list.length || !select.options)return;
  const current=[...select.options].map(o=>o.value);
  if(current.length===list.length && current.every((v,i)=>v===list[i]))return;
  while(select.options.length)select.remove(0);
  for(const name of list){const option=document.createElement('option');option.value=name;option.textContent=name;select.append(option);}
}
function showPurpose(purpose,draftId,state=null){
  currentPurpose=purpose||null;
  const actions=state?.draft?.payload?.actions||[];
  currentPurposes=Array.isArray(state?.purposes)?state.purposes.slice():[purpose||null];
  purposeDefaults=Array.isArray(state?.purpose_defaults)?state.purpose_defaults.slice():['Transfer'];
  const editing=document.activeElement===$('draftPurpose')||document.activeElement===$('draftPurposeNote');
  if(!editing || purposeDraftId!==draftId){
    $('draftPurpose').value=purpose?.category||purposeDefaults[0]||'Transfer';$('draftPurposeNote').value=purpose?.category==='Others'?purpose.note:'';
    $('draftPurposeNote').hidden=$('draftPurpose').value!=='Others';$('draftPurposeStatus').textContent='';
  }
  // A plan gets one purpose row per action; the first row is the original picker.
  const name=i=>{const a=actions[i];const p=(state?.payees||[]).find(x=>x.id===a?.payee_id);return p?.name||a?.payee_id||'';};
  $('draftPurposeFor').hidden=actions.length<2;$('draftPurposeFor').textContent=actions.length>1?`1. ${name(0)}`:'';
  $('draftPurposeLabel').textContent=actions.length>1?'Purposes':'Purpose';
  const more=$('draftPurposeMore');const editingMore=more.contains?.(document.activeElement);
  if(!(editingMore && purposeDraftId===draftId)){
    const rows=[];purposeRows=[];
    for(let i=1;i<actions.length;i++)rows.push(purposeRow(i,name(i)));
    more.replaceChildren(...rows);
  }
  purposeDraftId=draftId||'';
}
function purposeRow(i,payee){
  const row=document.createElement('div');row.className='purpose-extra';
  const caption=document.createElement('span');caption.className='purpose-for';caption.textContent=`${i+1}. ${payee}`;
  const select=document.createElement('select');select.id=`draftPurpose${i}`;select.setAttribute('aria-label',`Purpose for ${i+1}. ${payee}`);
  for(const value of [...($('draftPurpose').options||[])].map(o=>o.value)){const o=document.createElement('option');o.value=value;o.textContent=value;select.append(o);}
  const chosen=currentPurposes[i];select.value=chosen?.category||purposeDefaults[i]||'Transfer';
  const note=document.createElement('input');note.id=`draftPurposeNote${i}`;note.type='text';note.maxLength=80;note.autocomplete='off';
  note.placeholder='Write a short reason, then press Enter';note.setAttribute('aria-label',`Reason for Others, ${i+1}. ${payee}`);
  note.value=chosen?.category==='Others'?chosen.note:'';note.hidden=select.value!=='Others';
  const status=document.createElement('p');status.id=`draftPurposeStatus${i}`;status.className='small';status.setAttribute('role','status');
  purposeRows[i]={select,note,status};
  select.onchange=()=>{status.textContent='';note.hidden=select.value!=='Others';if(select.value==='Others'){note.focus?.();if(!note.value.trim())return;}saveActionPurpose(i);};
  note.onkeydown=e=>{if(e.key==='Enter'){e.preventDefault();saveActionPurpose(i);}};note.onchange=()=>saveActionPurpose(i);
  row.append(caption,select,note,status);return row;
}
async function saveActionPurpose(i){
  const draft=currentDraft,{select,note,status}=purposeRows[i]||{};
  if(!draft||!select)return;
  const category=select.value,text=(note?.value||'').trim();
  if(category===(purposeDefaults[i]||'Transfer') && !currentPurposes[i]){status.textContent='';return;}
  if(category==='Others' && !text){status.textContent='Write a short reason for "Others", then press Enter.';note?.focus?.();return;}
  status.textContent='Saving…';
  try{
    const result=await api('/api/draft/purpose',{session,draft_id:draft.payload.draft_id,category,note:category==='Others'?text:'',action:i});
    if(currentDraft?.payload.draft_id!==draft.payload.draft_id)return;
    currentPurposes[i]=result.purpose;status.textContent=`Saved: ${purposeLabel(result.purpose)}`;
  }catch(e){status.textContent=e.message;}
}
async function confirmSpokenAmount(check,button){
  const draft=currentDraft;if(!draft)return;
  if(button)button.disabled=true;
  try{
    await api('/api/draft/amount-check',{session,draft_id:draft.payload.draft_id,version:draft.current_version,action:check.action});
    await refresh();$('viewDraft').focus?.();
  }catch(e){if(button)button.disabled=false;error(e.message);}
}
async function savePurpose(){
  const draft=currentDraft,category=$('draftPurpose').value,note=$('draftPurposeNote').value.trim();
  $('draftPurposeNote').hidden=category!=='Others';
  if(!draft || !category){$('draftPurpose').value=currentPurpose?.category||'Transfer';return;}
  if(category===(purposeDefaults[0]||'Transfer') && !currentPurpose){$('draftPurposeStatus').textContent='';return;}  // already the default
  if(category==='Others' && !note){$('draftPurposeStatus').textContent='Write a short reason for "Others", then press Enter.';$('draftPurposeNote').focus();return;}
  $('draftPurposeStatus').textContent='Saving…';
  try{
    const result=await api('/api/draft/purpose',{session,draft_id:draft.payload.draft_id,category,note:category==='Others'?note:''});
    if(currentDraft?.payload.draft_id!==draft.payload.draft_id)return;
    currentPurpose=result.purpose;currentPurposes[0]=result.purpose;$('draftPurposeStatus').textContent=`Saved: ${purposeLabel(result.purpose)}`;
  }catch(e){$('draftPurposeStatus').textContent=/Unknown endpoint/.test(e.message)?'Could not save: the app server is an older version. Close it and start it again (Run-GUI.ps1 or Run-DemoGUI.ps1).':e.message;}
}
function purposeUnsaved(){
  for(let i=1;i<(currentDraft?.payload?.actions?.length||1);i++){
    const row=purposeRows[i];if(!row)continue;const c=row.select.value,p=currentPurposes[i];
    if(c && !(c===(purposeDefaults[i]||'Transfer') && !p) && (!p || p.category!==c || (c==='Others' && p.note!==(row.note.value||'').trim())))return true;
  }
  const category=$('draftPurpose').value;if(!category || (category===(purposeDefaults[0]||'Transfer') && !currentPurpose))return false;
  return !currentPurpose || currentPurpose.category!==category || (category==='Others' && currentPurpose.note!==$('draftPurposeNote').value.trim());
}
// Dates use this computer's clock and time zone, always in the same English format regardless of system language:
//   within two days  "Today, 26 Sep 2026 · 21:14" / "Yesterday, …" / "The day before yesterday, …"
//   otherwise        "Sunday, 20 Sep 2026 · 21:14"
// compact (narrow lists): "Today · 21:14", "Yesterday · 21:14", "Sun, 20 Sep 2026 · 21:14".
const MONTHS=['Jan','Feb','Mar','Apr','May','Jun','Jul','Aug','Sep','Oct','Nov','Dec'];
const WEEKDAYS=['Sunday','Monday','Tuesday','Wednesday','Thursday','Friday','Saturday'];
function formatWhen(value,{compact=false,now=Date.now()}={}){
  const d=new Date(typeof value==='string' && /^\d+$/.test(value)?Number(value):value);if(Number.isNaN(d.getTime()))return '';
  const today=new Date(now),dayStart=x=>new Date(x.getFullYear(),x.getMonth(),x.getDate()).getTime();
  const daysAgo=Math.round((dayStart(today)-dayStart(d))/86400000);
  const pad=n=>String(n).padStart(2,'0'),time=`${pad(d.getHours())}:${pad(d.getMinutes())}`;
  const date=`${d.getDate()} ${MONTHS[d.getMonth()]} ${d.getFullYear()}`;
  const relative=['Today','Yesterday','The day before yesterday'][daysAgo];
  if(compact)return relative?`${relative} · ${time}`:`${WEEKDAYS[d.getDay()].slice(0,3)}, ${date} · ${time}`;
  return `${relative||WEEKDAYS[d.getDay()]}, ${date} · ${time}`;
}
function statusTone(text){
  const t=String(text||'').toLowerCase();
  if(/cancel|fail|reject|expired|not finished/.test(t))return 'bad';
  if(/block|attention|pending|action required|changed|recogniz|transcript/.test(t))return 'warn';
  if(/settled|complet|succeed|checked|passed|validated(?! ·)|done/.test(t) && !/not validated/.test(t))return 'ok';
  return '';
}
function setTone(node,text){for(const c of ['tone-ok','tone-bad','tone-warn'])node.classList.remove(c);const tone=statusTone(text);if(tone)node.classList.add('tone-'+tone);}
function transferState(state,eligibility){if(state==='DRAFTED' && eligibility?.blocked)return 'Blocked';return ({DRAFTED:'Active draft',CANCELLED:'Cancelled',SUCCEEDED:'Settled'})[state] || state;}
async function loadTransfers({reset=false}={}) {
  if(reset)transferOffset=0;
  // One list of saved drafts (each payment once; a settled one opens its receipt), filtered by the status choice.
  const kind='drafts',ticket=++transferIssued,offset=transferOffset,query=$('transferSearch').value.trim(),filter=$('draftFilter').value||'ALL';
  const params=`?q=${encodeURIComponent(query)}&offset=${offset}&status=${encodeURIComponent(filter)}`;
  $('transferList').setAttribute('aria-busy','true');
  $('previousTransfers').disabled=true;$('nextTransfers').disabled=true;
  try {
    const data=await api('/api/'+kind+params);
    if(ticket!==transferIssued)return;
    const rows=data[kind];if(!Array.isArray(rows))throw Error('Transfer results are unavailable.');
    if(!rows.length && offset>0 && data.total<=offset){transferOffset=Math.max(0,Math.floor((Math.max(1,data.total)-1)/100)*100);return loadTransfers();}
    // CV14: a plan with several actions shows each one as its own entry (the conversation keeps them together).
    const entries=rows.flatMap(row=>Array.isArray(row.actions)&&row.actions.length>1?row.actions.map((a,i)=>({...row,amount_minor:a.amount_minor,payee_name:a.payee_name,kind:a.kind,purpose:a.purpose,partIndex:i,part:`Part ${i+1} of ${row.actions.length}`})):[row]);
    const cards=entries.map(row=>{
      // Row: category icon · recipient · "category · status" · date (and part) · amount on the right.
      const button=document.createElement('button');button.type='button';button.className='transfer-card txn-row';
      const icon=iconCircle(row.purpose||'Transfer',row.kind);icon.classList?.add('txn-row-icon');
      const payee=document.createElement('span');payee.className='txn-row-title';payee.textContent='To '+row.payee_name;payee.setAttribute('title',payee.textContent);
      const settled=row.state==='SUCCEEDED';
      const state=document.createElement('small');state.className='txn-row-state';state.textContent=`${transferState(row.state,row.eligibility)} · Version ${row.version}`;setTone(state,state.textContent);
      const date=document.createElement('small');date.className='txn-row-date';date.textContent=`${row.purpose||'Transfer'} · ${row.part?row.part+' · ':''}${formatWhen(settled&&row.settled_ms?row.settled_ms:row.updated_at,{compact:true})}`;
      const amount=document.createElement('strong');amount.className='txn-row-amount';setAmount(amount,row.amount_minor,settled?'−':'');
      if(row.eligibility?.blocked)button.classList.add('blocked-transfer');
      button.append(icon,payee,state,date,amount);
      button.setAttribute('aria-label',`${money(row.amount_minor)}. ${payee.textContent}.${row.part?` ${row.part}, paid together.`:''} ${row.purpose||'Transfer'}. ${state.textContent}. View details.`);
      button.onclick=()=>settled&&row.execution_id?openSavedTransfer('receipts',row.execution_id,null,null,row.partIndex??null):openSavedTransfer('drafts',row.id,null,null,row.partIndex??null);return button;
    });
    $('transferList').replaceChildren(...cards);if(reset)$('transferList').scrollTop=0;
    transferHasMore=!!data.has_more;
    $('transferStatus').textContent=rows.length?`${offset+1}–${offset+rows.length} of ${data.total} saved drafts.`:
      query?'No matching records. Try another name, amount or reference number.':filter==='ALL'?'No saved drafts yet. Prepare a transfer in the conversation.':'No drafts with this status. Try All saved drafts.';
    $('transferPages').hidden=!offset && !transferHasMore;$('previousTransfers').disabled=!offset;$('nextTransfers').disabled=!transferHasMore;
  } catch(e) {
    if(ticket===transferIssued){$('transferStatus').textContent='Could not refresh transfers. Any displayed results are from the previous request. Use Refresh transfers to retry.';transferHasMore=false;$('previousTransfers').disabled=!offset;}
  } finally {if(ticket===transferIssued)$('transferList').setAttribute('aria-busy','false');}
}
function closeSavedTransfer(){++savedTransferIssued;savedTransfer=null;savedTransferRequest=null;$('savedTransferDialog').close();}
async function openSavedTransfer(kind,id,version=null,compare=null,part=null) {
  const ticket=++savedTransferIssued;
  savedTransfer=null;savedTransferRequest={kind,id,version,compare,part};
  $('savedTransferTitle').textContent='Transaction details';
  $('savedTransferStatus').textContent='Loading saved details…';$('savedTransferDetails').hidden=true;$('retrySavedTransfer').hidden=true;
  if(!$('savedTransferDialog').open)$('savedTransferDialog').showModal();
  try {
    const path=kind==='drafts'?'/api/draft':'/api/receipt';
    const data=await api(path+'?id='+encodeURIComponent(id)+(version===null?'':'&version='+encodeURIComponent(version))+(compare===null?'':'&compare='+encodeURIComponent(compare)));
    if(ticket!==savedTransferIssued || !$('savedTransferDialog').open)return;
    if((kind==='drafts'?data.draft_id:data.execution_id)!==id || (version!==null && data.version!==Number(version)))throw Error('The saved record did not match. Refresh and try again.');
    savedTransfer={...data,kind};
    const info={purposes:data.purposes||[data.purpose],defaults:data.purpose_defaults||['Transfer'],kinds:data.kinds||['PERSON']};
    setAmount($('savedTransferAmount'),data.amount_minor);$('savedTransferSource').textContent=`${data.source.name} · ${data.source.id}`;
    $('savedTransferPayee').textContent=`${data.payee.name} · ending ${data.payee.ending}`;$('savedTransferPurpose').textContent=data.purposes?purposeSummary(data.purposes,data.purpose_defaults,'\n'):purposeLabel(data.purpose);
    const noFee=kind==='receipts' && data.fee_minor===0,fee=$('savedTransferFee');
    fee.textContent=money(data.fee_minor);fee.classList.toggle('fee-zero',noFee);
    if(noFee){
      fee.textContent='';const amount=document.createElement('span');amount.className='fee-zero-amount';amount.textContent=money(data.fee_minor);fee.append(amount);
      fee.setAttribute('aria-label','No fee');
    }else fee.removeAttribute('aria-label');
    const manyParts=renderParts('savedTransferActions',data.actions||[],info);
    $('savedPlanNote').hidden=!manyParts;$('savedPlanNote').textContent=manyParts?((data.constraints||[]).length?`Keeps at least ${money(data.constraints[0].amount_minor)} in ${data.constraints[0].account_name||data.constraints[0].account_id}. `:'')+'Approved once and paid together: all or nothing.':'';
    if(manyParts)$('savedTransferPayee').textContent=`${data.actions.length} recipients (listed above)`;
    const cat0=categoryOf(info.purposes,info.defaults,0);
    $('savedTransferHeadline').textContent=manyParts?`${data.actions.length} payments`:data.payee.name;
    $('savedTransferType').textContent=manyParts?`Combined payment · ${data.actions.length} parts`:TYPE_LABEL[info.kinds[0]]||TYPE_LABEL.PERSON;
    setIcon($('savedTransferIcon'),manyParts?'Transfer':cat0,manyParts?'PERSON':info.kinds[0]);heroIcons('savedTransfer',info,manyParts?data.actions.length:1);setIcon($('savedTransferCatIcon'),manyParts?'Transfer':cat0,manyParts?'PERSON':info.kinds[0]);
    if(manyParts)categoryLines('savedTransferCatIcon','savedTransferPurpose',info,data.actions.length);else categorySingle('savedTransferCatIcon','savedTransferPurpose',purposeLabel(info.purposes[0],cat0));
    const actionsAll=data.actions||[],single=part!==null && actionsAll.length>1 && actionsAll[part];
    if(single){
      // One part of a multi-part payment: shown like its own transfer, with its own purpose.
      const a=actionsAll[part];renderParts('savedTransferActions',[],info);$('savedPlanNote').hidden=true;
      setAmount($('savedTransferAmount'),a.amount_minor);$('savedTransferSource').textContent=`${a.source.name} · ${a.source.id}`;
      $('savedTransferPayee').textContent=`${a.payee.name} · ending ${a.payee.ending}`;$('savedTransferHeadline').textContent=a.payee.name;
      const cat=categoryOf(info.purposes,info.defaults,part);
      categorySingle('savedTransferCatIcon','savedTransferPurpose',purposeLabel(info.purposes?.[part],cat));$('savedTransferType').textContent=TYPE_LABEL[info.kinds?.[part]]||TYPE_LABEL.PERSON;
      setIcon($('savedTransferIcon'),cat,info.kinds?.[part]);heroIcons('savedTransfer',info,1);setIcon($('savedTransferCatIcon'),cat,info.kinds?.[part]);
    }
    const receipt=kind==='receipts',historical=!receipt && data.version!==data.current_version;
    showReference('savedTransfer',receipt?data.execution_id:null);
    $('savedTransferBadge').hidden=true;$('savedTransferBadge').textContent='';
    $('savedTransferState').textContent=receipt?'Settled':`${transferState(data.state,data.eligibility)}${historical?' · Historical version':''}`;setTone($('savedTransferState'),$('savedTransferState').textContent);
    $('savedTransferDate').textContent=formatWhen(receipt?data.created_ms:data.created_at);$('savedDateLabel').textContent=receipt?'Date':'Saved';
    $('savedTransferBalance').hidden=true;$('savedBalanceLabel').hidden=true;
    $('savedVersionForm').hidden=receipt || data.current_version===1;
    $('savedVersionHint').hidden=true;
    $('savedRuleMessage').hidden=!data.eligibility?.blocked;$('savedRuleMessage').textContent=data.eligibility?.blocked?issueText(data.eligibility.code,data.eligibility.message):'';
    if(!receipt){
      $('savedVersion').replaceChildren();
      for(let version=data.current_version;version>=1;version--){const option=document.createElement('option');option.value=String(version);option.textContent=`Version ${version}`;$('savedVersion').append(option);}
      $('savedVersion').value=String(data.version);$('savedVersionCount').textContent=`of ${data.current_version}`;
      setComparisonOptions(data.version,data.changes?.from_version);
    }
    $('savedChanges').hidden=!data.changes;$('savedChangesList').replaceChildren();
    if(data.changes){
      $('savedChangesTitle').textContent=`Changes: version ${data.changes.from_version} → ${data.changes.to_version}`;
      for(const change of data.changes.changes){const row=document.createElement('div');row.className='draft-change';const label=document.createElement('strong');label.textContent=change.label;const before=document.createElement('p');before.textContent='Before: '+change.before;const after=document.createElement('p');after.textContent='After: '+change.after;row.append(label,before,after);$('savedChangesList').append(row);}
      if(!data.changes.changes.length)$('savedChangesList').textContent='No financial fields changed between these versions.';
    }
    const done=receipt || data.state==='SUCCEEDED';
    $('savedTransferTitle').textContent='Transaction details';
    $('savedTransferNote').textContent=receipt?'':historical?'This is an older version. The conversation shows the latest one.':
      data.state==='SUCCEEDED'?'This transfer has settled.':data.state==='CANCELLED'?'This draft was cancelled. No money moved.':
      data.eligibility?.blocked?'Blocked by a safety rule. Open the conversation to change or cancel it.':'To review or change this draft, open its conversation.';
    $('savedTransferNote').hidden=!$('savedTransferNote').textContent;
    $('continueSavedTransfer').textContent=!receipt && !historical && data.state==='DRAFTED'?(data.eligibility?.blocked?'Open blocked draft in conversation':'Open current draft in conversation'):'Open conversation';
    $('continueSavedTransfer').disabled=false;$('savedTransferDetails').hidden=false;$('savedTransferStatus').textContent='';
  } catch(e){if(ticket===savedTransferIssued && $('savedTransferDialog').open){$('savedTransferStatus').textContent=e.message;$('retrySavedTransfer').hidden=false;}}
}
async function continueSavedTransfer(){
  const record=savedTransfer;if(!record)return;
  if(capture || pendingCapture || voice || review || reviewing || signing || pendingPayment || sendingText || switchingSession){$('savedTransferStatus').textContent='Finish the current recording, review or pending request before opening another conversation.';return;}
  const ticket=savedTransferIssued;$('continueSavedTransfer').disabled=true;
  try {
    await switchSession(record.session_id);
    if(ticket!==savedTransferIssued || !$('savedTransferDialog').open)return;
    const matching=record.kind==='drafts' && record.state==='DRAFTED' && record.version===record.current_version && currentDraft?.payload.draft_id===record.draft_id && currentDraft.current_version===record.version;
    closeSavedTransfer();closeTransfersDrawer();
    if(matching)$('draftDialog').showModal();
    else if(record.kind==='drafts' && record.state==='DRAFTED')error('Opened the latest conversation. Check the current draft before reviewing; saved versions are read only.');
  } catch(e){if(ticket===savedTransferIssued){$('savedTransferStatus').textContent=e.message;$('continueSavedTransfer').disabled=false;}}
}
$('draftFilter').onchange=()=>loadTransfers({reset:true});
$('transferSearchForm').onsubmit=e=>{e.preventDefault();return loadTransfers({reset:true});};
$('refreshTransfers').onclick=()=>loadTransfers();
$('previousTransfers').onclick=()=>{transferOffset=Math.max(0,transferOffset-100);return loadTransfers();};
$('nextTransfers').onclick=()=>{if(transferHasMore){transferOffset+=100;return loadTransfers();}};
$('closeSavedTransfer').onclick=closeSavedTransfer;
$('savedTransferDialog').oncancel=e=>{e.preventDefault();closeSavedTransfer();};
$('retrySavedTransfer').onclick=()=>{if(savedTransferRequest){const {kind,id,version,compare}=savedTransferRequest;return openSavedTransfer(kind,id,version,compare);}};
function setComparisonOptions(version,preferred){
  $('savedCompare').replaceChildren();
  $('savedCompare').hidden=version<=1;$('savedCompareLabel').hidden=version<=1;$('savedCompare').disabled=version<=1;
  for(let earlier=version-1;earlier>=1;earlier--){const option=document.createElement('option');option.value=String(earlier);option.textContent=`Version ${earlier}`;$('savedCompare').append(option);}
  $('savedCompare').value=version>1?String(preferred>=1 && preferred<version?preferred:version-1):'';
}
$('savedVersion').onchange=()=>{
  const version=Number($('savedVersion').value);
  if(Number.isInteger(version) && version>=1 && savedTransfer && version<=savedTransfer.current_version)setComparisonOptions(version,Number($('savedCompare').value));
};
$('savedVersion').oninput=$('savedVersion').onchange;
$('savedVersionForm').onsubmit=e=>{
  e.preventDefault();const data=savedTransfer;if(!data || data.kind!=='drafts')return;
  const version=Number($('savedVersion').value),raw=$('savedCompare').value,compare=raw===''?null:Number(raw);
  if(!Number.isInteger(version) || version<1 || version>data.current_version || (compare!==null && (!Number.isInteger(compare) || compare<1 || compare>=version))){$('savedTransferStatus').textContent='Choose a saved version and an earlier version to compare, or leave comparison empty.';return;}
  return openSavedTransfer('drafts',data.draft_id,version,compare);
};
$('continueSavedTransfer').onclick=continueSavedTransfer;
function focusTransferChange(){
  if(switchingSession || sendingText)return;
  $('draftDialog').close();
  const box=$('message'),actions=currentDraft?.payload?.actions||[];
  // Only a starter phrase is typed for the user; nothing is sent or changed until they send it.
  if(!box.value.trim()){
    const payee=actions.length>1?currentPayees.find(p=>p.id===actions[0].payee_id):null;
    box.value=actions.length>1?`Change the transfer to ${payee?payee.name:'…'} to SGD `:'Make it SGD ';
    try{box.setSelectionRange?.(box.value.length,box.value.length);}catch{}
    box.oninput?.();
  }
  $('draftRuleHint').hidden=false;
  $('draftRuleHint').textContent=actions.length>1?'Type the new amount for that transfer below and press Enter, or say which transfer to change.':'Type the new amount below and press Enter (for example “Make it SGD 500”), or cancel the draft.';
  try{box.scrollIntoView?.({block:'center',behavior:'smooth'});}catch{}
  box.focus();
}
$('cancelBlockedTransfer').onclick=()=>{if(switchingSession || sendingText)return;$('draftDialog').close();return send('Cancel it.').catch(e=>error(e.message));};
$('changeBlockedTransfer').onclick=focusTransferChange;
$('editBlockedDraft').onclick=focusTransferChange;

connectWorkspace();
