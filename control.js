// Color corrector — UI de contrôle embarquée (plugin). Montée par le shell Traitements via
// window.MXLPlugins.color_corrector.mount(el, vmid, ctx). Un seul éditeur actif à la fois
// (le shell démonte le précédent avant d'en monter un autre). Aucun changement d'endpoints :
// state/params/reset/input via le proxy /api/containers/<vmid>/plugin/*, presets via le
// stockage générique /api/plugins/color_corrector/store.
window.MXLPlugins = window.MXLPlugins || {};
window.MXLPlugins.color_corrector = (function () {
    let EL = null, VMID = null, TOAST = () => {};
    let state = null, presets = [], advanced = false, saveOpen = false;
    let pending = null, errorMsg = null, drag = null, pollTimer = null;

    const BASIC = [
        {key: "brightness", label: "Luminosité", min: -1, max: 1, step: 0.01, def: 0, unit: ""},
        {key: "contrast",   label: "Contraste",  min: 0,  max: 2, step: 0.01, def: 1, unit: ""},
        {key: "saturation", label: "Saturation", min: 0,  max: 3, step: 0.01, def: 1, unit: ""},
        {key: "gamma",      label: "Gamma",       min: 0.1, max: 10, step: 0.01, def: 1, unit: ""},
        {key: "hue",        label: "Teinte",      min: -180, max: 180, step: 1, def: 0, unit: "°"},
    ];
    const ADV = [
        {key: "gamma_r", label: "Gamma R", min: 0.1, max: 10, step: 0.01, def: 1, unit: ""},
        {key: "gamma_g", label: "Gamma V", min: 0.1, max: 10, step: 0.01, def: 1, unit: ""},
        {key: "gamma_b", label: "Gamma B", min: 0.1, max: 10, step: 0.01, def: 1, unit: ""},
    ];
    const CB_AXES = [{key:"r",short:"R"},{key:"g",short:"V"},{key:"b",short:"B"}];
    const CB_ZONES = [{key:"s",label:"Ombres"},{key:"m",label:"Midtones"},{key:"h",label:"Hautes lum."}];

    const api = (path, body) => fetch(`/api/containers/${VMID}/plugin${path}`, {
        method: body === undefined ? 'GET' : 'POST',
        headers: {'Content-Type': 'application/json'},
        body: body === undefined ? undefined : JSON.stringify(body || {}),
    });

    function esc(s){ return String(s==null?'':s).replace(/[&<>"']/g,c=>({'&':'&amp;','<':'&lt;','>':'&gt;','"':'&quot;',"'":'&#39;'}[c])); }
    function decimals(step){ const s=String(step); return s.includes('.')?s.split('.')[1].length:0; }
    function fmt(v, step, unit){ return v.toFixed(decimals(step)) + (unit||''); }
    function $(sel){ return EL ? EL.querySelector(sel) : null; }

    function showError(m){ errorMsg=m; renderError(); }
    function clearError(){ errorMsg=null; renderError(); }
    function renderError(){
        const slot=$('#cc-error-slot'); if(!slot) return;
        slot.innerHTML = errorMsg ? `<div class="cc-error" role="alert"><span>⚠ ${esc(errorMsg)}</span>
            <button type="button" aria-label="Fermer" onclick="__cc.clearError()">✕</button></div>` : '';
    }

    async function loadPresets(){
        // Stockage générique du plugin (value = params du preset).
        try {
            const r=await fetch('/api/plugins/color_corrector/store');
            if(r.ok){ presets=(await r.json()).map(p=>({id:p.id, name:p.name, params:p.value})); }
        } catch(e){ presets=[]; }
    }

    function userIsEditing(){
        if(drag) return true;
        const ae=document.activeElement;
        if(!ae || !EL || !EL.contains(ae)) return false;
        if(ae.tagName==='INPUT'||ae.tagName==='SELECT'||ae.tagName==='TEXTAREA') return true;
        if(ae.classList && ae.classList.contains('cc-knob-dial')) return true;
        return false;
    }

    async function refreshState({fullRebuild=false}={}){
        if(VMID==null || !EL) return;
        try {
            const r=await api('/state'); if(!r.ok) throw new Error('HTTP '+r.status);
            state=await r.json();
            if(fullRebuild || !$('.cc-toolbar')) render();
            else if(!userIsEditing()) updateStateBar();
        } catch(e){
            EL.innerHTML=`<div style="color:var(--status-stopped-fg)">Erreur : ${esc(e.message)}</div>`;
        }
    }

    function updateStateBar(){
        const bar=$('#cc-state-bar'); if(!bar||!state) return;
        const wire=state.input_shm||'';
        bar.innerHTML=`entrée : <code>${wire?esc(wire):'(non câblée)'}</code> · sortie : <code>${esc(state.shm_out||'')}</code>`;
    }

    // ─── Knob SVG + HTML ───────────────────────────────────
    function knobSvg(p01){
        const p=Math.max(0,Math.min(1,p01)), r=22, C=2*Math.PI*r, sweep=C*0.75, prog=sweep*p, ind=-135+p*270;
        return `<svg viewBox="0 0 56 56" aria-hidden="true">
            <g transform="rotate(135 28 28)">
                <circle cx="28" cy="28" r="${r}" fill="none" stroke="var(--border)" stroke-width="3" stroke-dasharray="${sweep} ${C}"/>
                <circle cx="28" cy="28" r="${r}" fill="none" stroke="var(--accent)" stroke-width="3" stroke-dasharray="${prog} ${C}" stroke-linecap="round"/>
            </g>
            <line x1="28" y1="28" x2="28" y2="9" stroke="var(--text-strong)" stroke-width="2" stroke-linecap="round" transform="rotate(${ind} 28 28)"/>
            <circle cx="28" cy="28" r="2.2" fill="var(--text-muted)"/></svg>`;
    }
    function knobHtml(p, value, def){
        const v=(value==null)?def:value, min=p.min, max=p.max, step=p.step, unit=p.unit||'';
        return `<div class="cc-knob" data-key="${p.key}" data-min="${min}" data-max="${max}" data-step="${step}" data-default="${def}" data-unit="${unit}">
            <div class="cc-knob-label">${esc(p.label)}</div>
            <div class="cc-knob-dial" role="slider" tabindex="0" aria-label="${esc(p.label)}"
                 aria-valuemin="${min}" aria-valuemax="${max}" aria-valuenow="${v}" aria-valuetext="${fmt(v,step,unit)}"
                 title="Glisser vertical, molette, flèches. Double-clic = réinit."
                 onpointerdown="__cc.knobDown(event,this)" ondblclick="__cc.knobReset(this)"
                 onwheel="__cc.knobWheel(event,this)" onkeydown="__cc.knobKey(event,this)">${knobSvg((v-min)/(max-min))}</div>
            <input type="number" class="cc-knob-value" min="${min}" max="${max}" step="${step}" value="${v}"
                   aria-label="${esc(p.label)}" oninput="__cc.knobNumberInput(this)" onchange="__cc.knobNumberCommit(this)"></div>`;
    }
    function knobApply(knob, v){
        const min=parseFloat(knob.dataset.min), max=parseFloat(knob.dataset.max),
              step=parseFloat(knob.dataset.step), unit=knob.dataset.unit||'';
        v=parseFloat(v.toFixed(decimals(step)));
        const dial=knob.querySelector('.cc-knob-dial'), num=knob.querySelector('.cc-knob-value');
        dial.innerHTML=knobSvg((v-min)/(max-min));
        dial.setAttribute('aria-valuenow',v); dial.setAttribute('aria-valuetext',fmt(v,step,unit));
        if(document.activeElement!==num) num.value=v;
    }
    function knobClamp(knob, v){ return Math.max(parseFloat(knob.dataset.min), Math.min(parseFloat(knob.dataset.max), v)); }

    function knobDown(e, dial){
        if(e.button!==undefined && e.button!==0) return;
        e.preventDefault();
        const knob=dial.parentNode, num=knob.querySelector('.cc-knob-value');
        drag={knob, dial, key:knob.dataset.key, startY:e.clientY, startValue:parseFloat(num.value),
              min:parseFloat(knob.dataset.min), max:parseFloat(knob.dataset.max), step:parseFloat(knob.dataset.step)};
        try { dial.setPointerCapture(e.pointerId); } catch(_){}
        dial.addEventListener('pointermove', knobMove);
        dial.addEventListener('pointerup', knobUp);
        dial.addEventListener('pointercancel', knobUp);
        dial.focus();
    }
    function knobMove(e){
        if(!drag) return;
        const dy=drag.startY-e.clientY, range=drag.max-drag.min;
        const vpp=e.shiftKey?drag.step:range/200;
        let v=knobClamp(drag.knob, drag.startValue+dy*vpp);
        knobApply(drag.knob, Math.round(v/drag.step)*drag.step);
    }
    function knobUp(){
        const s=drag; if(!s) return;
        s.dial.removeEventListener('pointermove', knobMove);
        s.dial.removeEventListener('pointerup', knobUp);
        s.dial.removeEventListener('pointercancel', knobUp);
        const v=parseFloat(s.knob.querySelector('.cc-knob-value').value);
        drag=null;
        if(!isNaN(v)) postParams({[s.key]: v});
    }
    function knobReset(dial){
        const knob=dial.parentNode||dial.closest('.cc-knob'), def=parseFloat(knob.dataset.default);
        knobApply(knob, def); postParams({[knob.dataset.key]: def});
    }
    function knobWheel(e, dial){
        e.preventDefault();
        const knob=dial.parentNode, step=parseFloat(knob.dataset.step), num=knob.querySelector('.cc-knob-value');
        const v=knobClamp(knob, parseFloat(num.value)+(e.deltaY<0?1:-1)*step*(e.shiftKey?10:1));
        knobApply(knob, v); postParams({[knob.dataset.key]: parseFloat(num.value)});
    }
    function knobKey(e, dial){
        const knob=dial.parentNode, step=parseFloat(knob.dataset.step), num=knob.querySelector('.cc-knob-value');
        const min=parseFloat(knob.dataset.min), max=parseFloat(knob.dataset.max), mult=e.shiftKey?10:1;
        let v=parseFloat(num.value), handled=true;
        switch(e.key){
            case 'ArrowUp': case 'ArrowRight': v+=step*mult; break;
            case 'ArrowDown': case 'ArrowLeft': v-=step*mult; break;
            case 'PageUp': v+=step*10; break;
            case 'PageDown': v-=step*10; break;
            case 'Home': v=min; break;
            case 'End': v=max; break;
            case 'Enter': case ' ': knobReset(dial); return;
            default: handled=false;
        }
        if(!handled) return;
        e.preventDefault();
        knobApply(knob, knobClamp(knob, v)); postParams({[knob.dataset.key]: parseFloat(num.value)});
    }
    function knobNumberInput(num){
        const knob=num.closest('.cc-knob'), v=parseFloat(num.value); if(isNaN(v)) return;
        const min=parseFloat(knob.dataset.min), max=parseFloat(knob.dataset.max), step=parseFloat(knob.dataset.step), unit=knob.dataset.unit||'';
        const dial=knob.querySelector('.cc-knob-dial'), clamped=Math.max(min,Math.min(max,v));
        dial.innerHTML=knobSvg((clamped-min)/(max-min));
        dial.setAttribute('aria-valuenow',v); dial.setAttribute('aria-valuetext',fmt(clamped,step,unit));
    }
    function knobNumberCommit(num){
        const knob=num.closest('.cc-knob'), v=parseFloat(num.value); if(isNaN(v)) return;
        const clamped=knobClamp(knob, v); knobApply(knob, clamped); postParams({[knob.dataset.key]: clamped});
    }

    // ─── Render ────────────────────────────────────────────
    function render(){
        const s=state||{}, p=s.params||{}, def=s.defaults||{};
        const presetsOpts=['<option value="">— Sélectionner un preset —</option>']
            .concat(presets.map(pr=>`<option value="${pr.id}">${esc(pr.name)}</option>`)).join('');
        const basicHtml=`<div class="cc-knob-row">${BASIC.map(pp=>knobHtml(pp,p[pp.key],def[pp.key]??pp.def)).join('')}</div>`;
        const advHtml=!advanced?'':`<div id="cc-adv-section">
            <div class="cc-section"><h4>Gamma par canal</h4>
                <div class="cc-knob-row">${ADV.map(pp=>knobHtml(pp,p[pp.key],def[pp.key]??pp.def)).join('')}</div></div>
            <div class="cc-section"><h4>Color Balance</h4><div class="cc-cb-grid">
                ${CB_ZONES.map(z=>`<div class="cc-cb-zone"><h5>${z.label}</h5><div class="cc-knob-row cc-knob-row-tight">
                    ${CB_AXES.map(ax=>{const key=`cb_${ax.key}${z.key}`; return knobHtml({key,label:ax.short,min:-1,max:1,step:0.01,def:0,unit:""}, p[key], 0);}).join('')}
                </div></div>`).join('')}
            </div></div></div>`;
        const saveBlock=saveOpen?`<div class="cc-inline-prompt" role="group" aria-label="Enregistrer le preset">
            <label for="cc-preset-name" style="font-size:0.88em">Nom :</label>
            <input id="cc-preset-name" type="text" placeholder="Mon preset"
                   onkeydown="if(event.key==='Enter')__cc.savePresetConfirm();if(event.key==='Escape')__cc.savePresetCancel()">
            <button type="button" class="btn btn-green" onclick="__cc.savePresetConfirm()">Enregistrer</button>
            <button type="button" class="btn" onclick="__cc.savePresetCancel()">Annuler</button></div>`
        :`<select id="cc-preset-select" aria-label="Preset à charger">${presetsOpts}</select>
            <button type="button" class="btn btn-blue" onclick="__cc.loadPreset()">Charger</button>
            <button type="button" class="btn btn-green" onclick="__cc.savePresetOpen()">Enregistrer sous…</button>
            ${pending==='delete-preset'?`<span class="cc-confirm" role="status">Supprimer ce preset ?
                <button type="button" class="btn btn-red" onclick="__cc.deletePresetConfirm()">Confirmer</button>
                <button type="button" class="btn" onclick="__cc.cancelPending()">Annuler</button></span>`
            :`<button type="button" class="btn" onclick="__cc.deletePresetAsk()" title="Supprimer le preset sélectionné">Supprimer preset</button>`}
            <span class="cc-toolbar-spacer"></span>
            ${pending==='reset-all'?`<span class="cc-confirm" role="status">Tout réinitialiser ?
                <button type="button" class="btn btn-orange" onclick="__cc.resetAllConfirm()">Confirmer</button>
                <button type="button" class="btn" onclick="__cc.cancelPending()">Annuler</button></span>`
            :`<button type="button" class="btn btn-orange" onclick="__cc.resetAllAsk()">Tout réinitialiser</button>`}`;
        const advToggle=`<div class="cc-adv-toggle-row">
            <button type="button" class="cc-adv-toggle ${advanced?'is-open':''}" aria-expanded="${advanced?'true':'false'}"
                    aria-controls="cc-adv-section" onclick="__cc.toggleAdvanced(${!advanced})">
                <span class="cc-adv-toggle-chevron" aria-hidden="true">▾</span>
                <span>${advanced?'Masquer le mode avancé':'Afficher le mode avancé'}</span></button></div>`;
        EL.innerHTML=`<div id="cc-state-bar" class="cc-state-bar"></div><div id="cc-error-slot"></div>
            <div class="cc-toolbar">${saveBlock}</div>
            <div class="cc-input-wire"><label for="cc-input-shm" style="font-size:0.88em">Câblage entrée :</label>
                <input id="cc-input-shm" type="text" value="${esc(s.input_shm||'')}" placeholder="nom du shm (sans /dev/shm/)">
                <button type="button" class="btn btn-blue" onclick="__cc.applyWire()">Appliquer</button></div>
            <div class="cc-section"><h4>Réglages de base</h4>${basicHtml}</div>
            ${advToggle}${advHtml}
            <p class="cc-hint">Glisser le bouton vers le haut / bas, molette ou flèches clavier. Double-clic = réinitialiser.</p>`;
        updateStateBar(); renderError();
        if(saveOpen){ const inp=$('#cc-preset-name'); if(inp) inp.focus(); }
    }

    function toggleAdvanced(on){ advanced=on; render(); }
    function cancelPending(){ pending=null; render(); }

    async function postParams(patch){
        if(VMID==null) return;
        try { const r=await api('/params', patch); if(!r.ok) throw new Error('HTTP '+r.status); clearError(); }
        catch(e){ showError("Envoi des paramètres : "+e.message); }
    }
    function resetAllAsk(){ pending='reset-all'; render(); }
    async function resetAllConfirm(){
        pending=null;
        try { const r=await api('/reset', {}); if(!r.ok) throw new Error('HTTP '+r.status); clearError(); }
        catch(e){ showError("Réinitialisation : "+e.message); }
        refreshState({fullRebuild:true});
    }
    async function applyWire(){
        const v=($('#cc-input-shm').value||'').trim();
        try { const r=await api('/input', {shm:v||null}); if(!r.ok) throw new Error('HTTP '+r.status); clearError(); }
        catch(e){ showError("Câblage entrée : "+e.message); }
        refreshState({fullRebuild:true});
    }
    async function loadPreset(){
        const sel=$('#cc-preset-select'), pid=parseInt(sel.value);
        if(!pid){ showError("Sélectionne d'abord un preset à charger."); return; }
        const pr=presets.find(x=>x.id===pid); if(!pr) return;
        await postParams(pr.params||{}); refreshState({fullRebuild:true});
    }
    function savePresetOpen(){ saveOpen=true; render(); }
    function savePresetCancel(){ saveOpen=false; render(); }
    async function savePresetConfirm(){
        const inp=$('#cc-preset-name'), name=(inp&&inp.value||'').trim();
        if(!name){ showError("Donne un nom au preset."); return; }
        const params=(state&&state.params)||{};
        try {
            const r=await fetch('/api/plugins/color_corrector/store', {method:'POST', headers:{'Content-Type':'application/json'}, body:JSON.stringify({name, value:params})});
            if(!r.ok){ const j=await r.json().catch(()=>({})); throw new Error(j.error||('HTTP '+r.status)); }
            clearError();
        } catch(e){ showError("Enregistrement du preset : "+e.message); return; }
        saveOpen=false; await loadPresets(); render();
    }
    function deletePresetAsk(){
        const sel=$('#cc-preset-select');
        if(!sel||!parseInt(sel.value)){ showError("Sélectionne d'abord un preset à supprimer."); return; }
        pending='delete-preset'; render();
    }
    async function deletePresetConfirm(){
        const sel=$('#cc-preset-select'), pid=parseInt(sel&&sel.value);
        pending=null; if(!pid){ render(); return; }
        try { const r=await fetch(`/api/plugins/color_corrector/store/${pid}`, {method:'DELETE'}); if(!r.ok) throw new Error('HTTP '+r.status); clearError(); }
        catch(e){ showError("Suppression du preset : "+e.message); }
        await loadPresets(); render();
    }

    function mount(el, vmid, ctx){
        if(pollTimer){ clearInterval(pollTimer); pollTimer=null; }
        EL=el; VMID=vmid; TOAST=(ctx&&ctx.toast)||(()=>{});
        state=null; presets=[]; advanced=false; saveOpen=false; pending=null; errorMsg=null; drag=null;
        loadPresets().then(()=>refreshState({fullRebuild:true}));
        pollTimer=setInterval(()=>refreshState({fullRebuild:false}), 3000);
    }
    function unmount(){ if(pollTimer){ clearInterval(pollTimer); pollTimer=null; } EL=null; VMID=null; }

    const exp = {mount, unmount, clearError, knobDown, knobReset, knobWheel, knobKey,
        knobNumberInput, knobNumberCommit, toggleAdvanced, cancelPending, applyWire,
        loadPreset, savePresetOpen, savePresetCancel, savePresetConfirm,
        deletePresetAsk, deletePresetConfirm, resetAllAsk, resetAllConfirm};
    window.__cc = exp;   // raccourci pour les handlers inline du fragment
    return exp;
})();
