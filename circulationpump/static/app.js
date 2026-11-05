const $ = selector => document.querySelector(selector);
let selected = 'depump_port';
let state = null;
let busy = false;
let dutySelection = null;
let displayedDuty = null;
const storageKey = 'circulationpump.sequence.v1';
const statusNames = {0:'External force overspeed',1:'Reserved',6:'Voltage rollback',16:'Normal operation',17:'Manufacturer diagnostics',20:'Unable to start',22:'Overvoltage shutdown',23:'Undervoltage shutdown',25:'Overtemperature shutdown',27:'Current limited',28:'Maximum control effort',29:'Torque limited',30:'Temperature rollback',31:'Status unavailable'};

function error(message) {
  $('#error').textContent = message || '';
  $('#error').hidden = !message;
}

async function api(path, body) {
  const response = await fetch(`/api/${path}`, body === undefined ? {cache:'no-store'} : {
    method:'POST', headers:{'Content-Type':'application/json'}, body:JSON.stringify(body)
  });
  const value = await response.json();
  if (!response.ok) throw new Error(value.error || `HTTP ${response.status}`);
  return value;
}

async function action(path, body) {
  busy = true;
  render();
  try {
    const result = await api(path, body);
    if (path === 'scan' && result.pumps.length && !result.pumps.some(pump => pump.name === selected)) selected = result.pumps[0].name;
    error(null);
    state = await api('state');
  } catch (failure) { error(failure.message); }
  finally { busy = false; render(); }
}

function readPlan() {
  const form = $('#sequence-form');
  const plan = Object.fromEntries(['start','end','step','low_time','high_time','step_time','repeats'].map(key => [key, Number(form.elements[key].value)]));
  plan.return_trip = form.elements.return_trip.checked;
  if (!Object.values(plan).every(value => typeof value === 'boolean' || Number.isFinite(value))) throw new Error('Sequence values must be finite');
  for (const key of ['start','end']) if (plan[key] < 0 || plan[key] > 100) throw new Error('Start and end must be between 0 and 100%');
  if (plan.step < 0.5 || plan.step > 100 || [plan.low_time,plan.high_time,plan.step_time].some(value => value < 0.1 || value > 86400) || !Number.isInteger(plan.repeats) || plan.repeats < 1 || plan.repeats > 10000) throw new Error('Check step, hold times, and repeats');
  return plan;
}

function levelsFor(plan) {
  const quantize = value => Math.floor(value * 2 + 0.5) / 2;
  const start = quantize(plan.start), end = quantize(plan.end);
  const direction = end >= start ? 1 : -1;
  const levels = [start];
  let current = start;
  while (direction * (end - current) > plan.step) {
    current += direction * plan.step;
    const value = quantize(current);
    if (value !== levels.at(-1)) levels.push(value);
  }
  if (end !== levels.at(-1)) levels.push(end);
  if (plan.return_trip) levels.push(...levels.slice(0,-1).reverse());
  const low = Math.min(start,end), high = Math.max(start,end);
  const durations = low === high ? levels.map(() => plan.low_time + plan.high_time)
    : levels.map(level => level === low ? plan.low_time : level === high ? plan.high_time : plan.step_time);
  return {levels,durations};
}

function preview() {
  try {
    const plan = readPlan(), sequence = levelsFor(plan);
    localStorage.setItem(storageKey, JSON.stringify(plan));
    const seconds = sequence.durations.reduce((sum,duration) => sum + duration, 0) * plan.repeats;
    $('#preview').textContent = `${sequence.levels.length} steps / pass · ${seconds.toLocaleString()} s total · off at finish`;
    const passDuration = sequence.durations.reduce((sum,duration) => sum + duration, 0);
    const chartLeft = 38, chartWidth = 600 - chartLeft;
    const passWidth = chartWidth / plan.repeats;
    let elapsed = 0;
    const points = sequence.levels.flatMap((value,index) => {
      const start = elapsed / passDuration * passWidth;
      elapsed += sequence.durations[index];
      const end = elapsed / passDuration * passWidth;
      return [`${start},${105-value}`, `${end},${105-value}`];
    }).join(' ');
    const grid = [100,75,50,25,0].map(value => {
      const y = 105 - value;
      return `<line class="grid-line" x1="${chartLeft}" y1="${y}" x2="600" y2="${y}"/><text class="axis-label" x="${chartLeft - 6}" y="${y + 3}">${value}%</text>`;
    }).join('');
    $('#plot').innerHTML = `${grid}<defs><pattern id="sequence-pass" x="${chartLeft}" width="${passWidth}" height="110" patternUnits="userSpaceOnUse"><polyline points="${points}" fill="none" stroke="#073f87" stroke-width="2" vector-effect="non-scaling-stroke"/></pattern></defs><rect x="${chartLeft}" width="${chartWidth}" height="110" fill="url(#sequence-pass)"/>`;
  } catch (failure) { $('#preview').textContent = failure.message; $('#plot').replaceChildren(); }
}

function applyPlan(plan) {
  if (!plan || typeof plan !== 'object' || Array.isArray(plan)) throw new Error('Script must be a JSON object');
  const keys = ['start','end','step','dwell','low_time','high_time','step_time','repeats','return_trip'];
  if (Object.keys(plan).some(key => !keys.includes(key))) throw new Error('Unknown script field');
  for (const key of keys) if (key in plan) {
    if (key === 'dwell') continue;
    if (key === 'return_trip') {
      if (typeof plan[key] !== 'boolean') throw new Error('return_trip must be a boolean');
      $('#sequence-form').elements[key].checked = plan[key];
    } else {
      if (typeof plan[key] !== 'number' || !Number.isFinite(plan[key])) throw new Error(`${key} must be a number`);
      $('#sequence-form').elements[key].value = plan[key];
    }
  }
  if ('dwell' in plan) {
    for (const key of ['low_time','high_time','step_time']) {
      if (!(key in plan)) $('#sequence-form').elements[key].value = plan.dwell;
    }
  }
  readPlan();
  preview();
}

function metric(selector, value, unit) {
  const element = $(selector);
  element.replaceChildren(document.createTextNode(value == null ? '--' : Number(value.toFixed(1)).toLocaleString()));
  const suffix = document.createElement('span');
  suffix.textContent = unit;
  element.append(suffix);
}

function render() {
  if (!state) return;
  if (!busy) $('#arm').checked = state.armed;
  $('#arm').disabled = busy || state.scanning || Boolean(state.error);
  $('#scan').disabled = busy || state.armed || state.scanning || Boolean(state.error);
  $('#scan').textContent = state.scanning ? 'Scanning...' : 'Scan pumps';
  $('#scan-result').hidden = !state.scanning && !state.scan_result;
  $('#scan-result').textContent = state.scanning ? 'Listening for pump status...' : state.scan_result ? `${state.scan_result.names.length} pump address(es) found` : '';
  $('[data-run]').disabled = !state.armed || busy;
  document.querySelectorAll('[data-run]').forEach(button => button.disabled = !state.armed || busy);
  $('#connection').textContent = state.simulated ? 'SIMULATED BUS' : 'LIVE CAN';
  $('#source').textContent = `Source 0x${state.source.toString(16).toUpperCase().padStart(2,'0')}`;
  const container = $('#pumps');
  if ([...container.children].map(button => button.dataset.pump).join(',') !== state.pumps.map(pump => pump.name).join(',')) {
    container.replaceChildren();
    state.pumps.forEach(pump => {
      const button = document.createElement('button');
      button.className = 'pump'; button.dataset.pump = pump.name;
      const label = document.createElement('strong');
      const icon = document.createElement('img'); icon.className = 'pump-icon'; icon.src = '/pump.svg'; icon.alt = ''; icon.hidden = true;
      const name = document.createElement('span'); name.textContent = pump.label;
      label.append(icon, name);
      const details = document.createElement('small'); details.append(document.createElement('span'), document.createElement('span'));
      button.append(label, details);
      button.addEventListener('click', () => { selected = pump.name; render(); });
      container.append(button);
    });
  }
  state.pumps.forEach(pump => {
    const button = container.querySelector(`[data-pump="${pump.name}"]`);
    button.classList.toggle('selected', pump.name === selected);
    button.setAttribute('aria-pressed', pump.name === selected);
    button.querySelector('small span').textContent = `0x${pump.address.toString(16).toUpperCase()}`;
    const online = pump.status_age !== null && pump.status_age <= 3;
    button.querySelector('small span:last-child').textContent = pump.sequence ? 'Cycling' : pump.commanded_duty ? `${pump.commanded_duty}% cmd` : online ? 'Online' : 'Not seen';
    const running = Boolean(pump.sequence || pump.commanded_duty > 0);
    button.querySelector('.pump-icon').hidden = !running;
    button.classList.toggle('running', running);
  });
  const pump = state.pumps.find(pump => pump.name === selected);
  const dutyInput = $('#duty'), dutySlider = $('#duty-slider');
  const dutyInputActive = document.activeElement === dutyInput || document.activeElement === dutySlider;
  if (selected !== dutySelection || (!dutyInputActive && pump.commanded_duty !== displayedDuty)) {
    const duty = pump.commanded_duty ?? 0;
    dutyInput.value = duty;
    dutySlider.value = duty;
    dutySelection = selected;
    displayedDuty = pump.commanded_duty;
  }
  const fresh = pump.status_age !== null && pump.status_age <= 3;
  const heartbeat = $('#heartbeat');
  heartbeat.classList.toggle('online', fresh);
  heartbeat.classList.toggle('offline', !fresh);
  heartbeat.textContent = fresh ? 'Heartbeat: active' : pump.status_age === null ? 'Heartbeat: waiting' : 'Heartbeat: lost';
  $('#pump-name').textContent = pump.label;
  $('#pump-address').textContent = `${pump.name} / 0x${pump.address.toString(16).toUpperCase()}`;
  $('#mode').textContent = pump.sequence ? 'Sequence running' : pump.commanded_duty ? 'Manual command' : 'Idle';
  metric('#commanded', pump.commanded_duty, '%');
  metric('#measured', fresh ? pump.status?.measured_duty : null, '%');
  metric('#rpm', fresh ? pump.status?.rpm : null, 'rpm');
  metric('#power', fresh ? pump.status?.power_w : null, 'W');
  $('#status').textContent = fresh ? `${['Motor off','Forward','Reverse','Direction unavailable'][pump.status.direction]} / ${statusNames[pump.status.controller_status] || `Controller status ${pump.status.controller_status}`}` : 'No recent pump status';
  $('#age').textContent = pump.status_age === null ? '' : `${pump.status_age.toFixed(1)} s ago`;
  $('#job').hidden = !pump.sequence;
  if (pump.sequence) {
    $('#progress').value = 100 * (1 - pump.sequence.remaining / pump.sequence.duration);
    $('#remaining').textContent = `${pump.sequence.remaining.toFixed(1)} s remaining / ${pump.sequence.repeats} pass(es)`;
  }
  if (state.error) error(`CAN error: ${state.error}. Restart the tool.`);
  if (state.log.length) {
    $('#log').replaceChildren(...state.log.slice(-20).reverse().map(entry => {
      const row = document.createElement('li'), time = document.createElement('time'), message = document.createElement('span');
      time.textContent = new Date(entry.time * 1000).toLocaleTimeString(); message.textContent = entry.message;
      row.append(time, message); return row;
    }));
  }
}

$('#arm').addEventListener('change', () => {
  const armed = $('#arm').checked;
  if (armed && !confirm('Confirm the pump is disconnected from the RCU. Enable pump control?')) { $('#arm').checked = false; return; }
  action('arm', {armed, confirmed:armed});
});
$('#stop-all').addEventListener('click', () => action('stop', {}));
$('#scan').addEventListener('click', () => action('scan', {seconds:3}));
$('#stop-pump').addEventListener('click', () => action('stop', {pump:selected}));
$('#duty-slider').addEventListener('input', event => $('#duty').value = event.target.value);
$('#duty').addEventListener('input', event => $('#duty-slider').value = event.target.value);
$('#manual-form').addEventListener('submit', event => { event.preventDefault(); action('manual', {pump:selected, duty:Number($('#duty').value)}); });
$('#sequence-form').addEventListener('input', preview);
$('#sequence-form').addEventListener('submit', event => {
  event.preventDefault();
  try { action('sequence', {pump:selected, ...readPlan()}); } catch (failure) { error(failure.message); }
});
$('#save-script').addEventListener('click', () => {
  try {
    const url = URL.createObjectURL(new Blob([JSON.stringify(readPlan(), null, 2) + '\n'], {type:'application/json'}));
    const link = document.createElement('a'); link.href = url; link.download = 'pump-cycle.json'; link.click(); URL.revokeObjectURL(url);
  } catch (failure) { error(failure.message); }
});
$('#load-script').addEventListener('click', () => $('#script-file').click());
$('#script-file').addEventListener('change', async event => {
  try { const file = event.target.files[0]; if (file) { applyPlan(JSON.parse(await file.text())); error(null); } }
  catch (failure) { error(failure.message); }
  finally { event.target.value = ''; }
});

function release() {
  if (state?.armed) navigator.sendBeacon('/api/stop', new Blob(['{}'], {type:'application/json'}));
}
window.addEventListener('pagehide', release);
document.addEventListener('visibilitychange', () => { if (document.hidden) release(); });
try { const saved = localStorage.getItem(storageKey); if (saved) applyPlan(JSON.parse(saved)); } catch { localStorage.removeItem(storageKey); }
preview();
async function poll() {
  try {
    if (!document.hidden) await api('heartbeat', {});
    state = await api('state'); render();
  } catch (failure) {
    $('#connection').textContent = 'DISCONNECTED';
    document.querySelectorAll('[data-run]').forEach(button => button.disabled = true);
    error(failure.message);
  } finally { setTimeout(poll, 500); }
}
poll();