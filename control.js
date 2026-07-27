// SPDX-License-Identifier: GPL-3.0-or-later
// Copyright (C) 2026 BOBI SAS, France
// Auteur : Cyril Mazouer, pour le compte de BOBI SAS
// Distribué sous licence GNU GPL v3 (ou ultérieure) ; voir le fichier LICENSE.

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
    const GLOW = [
        {key: "glow",        label: "Intensité", min: 0,   max: 2,  step: 0.01, def: 0,   unit: ""},
        {key: "glow_thresh", label: "Seuil",     min: 0,   max: 1,  step: 0.01, def: 0.7, unit: ""},
        {key: "glow_radius", label: "Rayon",     min: 1,   max: 64, step: 1,    def: 8,   unit: "px"},
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
        if(ae.classList && ae.classList.contains('ctl-knob-hit')) return true;
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
    // Tracé délégué au catalogue (static/js/controls.js) : le dessin d'un rotatif n'a pas à être
    // réécrit par chaque plugin. La variante est lue sur l'élément (--ctl-knob-draw), donc changer
    // d'aspect ne demandera qu'un changement de classe, pas une réécriture de SVG.
    function knobSvg(p01, el, def01){
        const k = (el && window.MXLControls) ? window.MXLControls.knobKind(el) : 'arc';
        return window.MXLControls.knobSvg(k, p01, def01);
    }
    function knobHtml(p, value, def){
        const v=(value==null)?def:value, min=p.min, max=p.max, step=p.step, unit=p.unit||'';
        return `<div class="ctl-knob ctl-knob--arc" data-key="${p.key}" data-min="${min}" data-max="${max}" data-step="${step}" data-default="${def}" data-unit="${unit}">
            <div class="ctl-knob-name">${esc(p.label)}</div>
            <div class="ctl-knob-hit" role="slider" tabindex="0" aria-label="${esc(p.label)}"
                 aria-valuemin="${min}" aria-valuemax="${max}" aria-valuenow="${v}" aria-valuetext="${fmt(v,step,unit)}"
                 title="Glisser vertical, molette, flèches. Double-clic = réinit."
                 onpointerdown="__cc.knobDown(event,this)" ondblclick="__cc.knobReset(this)"
                 onwheel="__cc.knobWheel(event,this)" onkeydown="__cc.knobKey(event,this)">${knobSvg((v-min)/(max-min), null, (def-min)/(max-min))}</div>
            <input type="number" class="ctl-knob-val" min="${min}" max="${max}" step="${step}" value="${v}"
                   aria-label="${esc(p.label)}" oninput="__cc.knobNumberInput(this)" onchange="__cc.knobNumberCommit(this)">
            <button type="button" class="ctl-knob-reset" tabindex="-1"
                    title="Remettre à la valeur par défaut (${def}${unit})"
                    aria-label="Remettre ${esc(p.label)} à sa valeur par défaut"
                    onclick="__cc.knobReset(this)">↺</button></div>`;
    }
    function knobApply(knob, v){
        const min=parseFloat(knob.dataset.min), max=parseFloat(knob.dataset.max),
              step=parseFloat(knob.dataset.step), unit=knob.dataset.unit||'';
        v=parseFloat(v.toFixed(decimals(step)));
        const dial=knob.querySelector('.ctl-knob-hit'), num=knob.querySelector('.ctl-knob-val');
        dial.innerHTML=knobSvg((v-min)/(max-min), knob, (parseFloat(knob.dataset.default)-min)/(max-min));
        dial.setAttribute('aria-valuenow',v); dial.setAttribute('aria-valuetext',fmt(v,step,unit));
        if(document.activeElement!==num) num.value=v;
    }
    function knobClamp(knob, v){ return Math.max(parseFloat(knob.dataset.min), Math.min(parseFloat(knob.dataset.max), v)); }

    function knobDown(e, dial){
        if(e.button!==undefined && e.button!==0) return;
        e.preventDefault();
        const knob=dial.parentNode, num=knob.querySelector('.ctl-knob-val');
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
        const v=parseFloat(s.knob.querySelector('.ctl-knob-val').value);
        drag=null;
        if(!isNaN(v)) postParams({[s.key]: v});
    }
    function knobReset(el){
        // Appelé depuis le cadran (double-clic, Entrée) OU depuis le bouton de remise à zéro :
        // on remonte au rotatif plutôt que de supposer un parent direct.
        const knob=el.closest('.ctl-knob')||el.parentNode, def=parseFloat(knob.dataset.default);
        knobApply(knob, def); postParams({[knob.dataset.key]: def});
    }
    function knobWheel(e, dial){
        e.preventDefault();
        const knob=dial.parentNode, step=parseFloat(knob.dataset.step), num=knob.querySelector('.ctl-knob-val');
        const v=knobClamp(knob, parseFloat(num.value)+(e.deltaY<0?1:-1)*step*(e.shiftKey?10:1));
        knobApply(knob, v); postParams({[knob.dataset.key]: parseFloat(num.value)});
    }
    function knobKey(e, dial){
        const knob=dial.parentNode, step=parseFloat(knob.dataset.step), num=knob.querySelector('.ctl-knob-val');
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
        const knob=num.closest('.ctl-knob'), v=parseFloat(num.value); if(isNaN(v)) return;
        const min=parseFloat(knob.dataset.min), max=parseFloat(knob.dataset.max), step=parseFloat(knob.dataset.step), unit=knob.dataset.unit||'';
        const dial=knob.querySelector('.ctl-knob-hit'), clamped=Math.max(min,Math.min(max,v));
        // Le repère de défaut doit être passé ICI aussi : sans lui, saisir la valeur au clavier
        // effaçait le trait sur l'arc jusqu'au prochain rendu complet.
        dial.innerHTML=knobSvg((clamped-min)/(max-min), knob, (parseFloat(knob.dataset.default)-min)/(max-min));
        dial.setAttribute('aria-valuenow',v); dial.setAttribute('aria-valuetext',fmt(clamped,step,unit));
    }
    function knobNumberCommit(num){
        const knob=num.closest('.ctl-knob'), v=parseFloat(num.value); if(isNaN(v)) return;
        const clamped=knobClamp(knob, v); knobApply(knob, clamped); postParams({[knob.dataset.key]: clamped});
    }

    // ─── Render ────────────────────────────────────────────
    function render(){
        const s=state||{}, p=s.params||{}, def=s.defaults||{};
        const presetsOpts=['<option value="">— Sélectionner un preset —</option>']
            .concat(presets.map(pr=>`<option value="${pr.id}">${esc(pr.name)}</option>`)).join('');
        const basicHtml=`<div class="cc-knob-row">${BASIC.map(pp=>knobHtml(pp,p[pp.key],def[pp.key]??pp.def)).join('')}</div>`;
        const glowOn = ((p.glow_enabled ?? def.glow_enabled ?? 1) != 0);
        const advHtml=!advanced?'':`<div id="cc-adv-section">
            <div class="cc-section"><h4>Gamma par canal</h4>
                <div class="cc-knob-row">${ADV.map(pp=>knobHtml(pp,p[pp.key],def[pp.key]??pp.def)).join('')}</div></div>
            <div class="cc-section"><h4>Color Balance</h4><div class="cc-cb-grid">
                ${CB_ZONES.map(z=>`<div class="cc-cb-zone"><h5>${z.label}</h5><div class="cc-knob-row cc-knob-row-tight">
                    ${CB_AXES.map(ax=>{const key=`cb_${ax.key}${z.key}`; return knobHtml({key,label:ax.short,min:-1,max:1,step:0.01,def:0,unit:""}, p[key], 0);}).join('')}
                </div></div>`).join('')}
            </div></div>
            <div class="cc-section"><h4>Glow / Bloom
                <label class="cc-glow-switch"><input type="checkbox" class="ios-toggle" ${glowOn?'checked':''}
                       aria-label="Activer le glow" onchange="__cc.setGlow(this.checked)"></label></h4>
                <div class="cc-knob-row ${glowOn?'':'cc-glow-off'}">${GLOW.map(pp=>knobHtml(pp,p[pp.key],def[pp.key]??pp.def)).join('')}</div></div>
            </div>`;
        const saveBlock=saveOpen?`<div class="cc-inline-prompt" role="group" aria-label="Enregistrer le preset">
            <label for="cc-preset-name" style="font-size:0.88em">Nom :</label>
            <input id="cc-preset-name" type="text" placeholder="Mon preset"
                   onkeydown="if(event.key==='Enter')__cc.savePresetConfirm();if(event.key==='Escape')__cc.savePresetCancel()">
            <button type="button" class="btn btn-green" onclick="__cc.savePresetConfirm()">Enregistrer</button>
            <button type="button" class="btn" onclick="__cc.savePresetCancel()">Annuler</button></div>`
        :`<select class="ctl-select" id="cc-preset-select" aria-label="Preset à charger">${presetsOpts}</select>
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
        // Un rendu DÉTRUIT tout le DOM (innerHTML). Si un geste est en cours — et au doigt c'est
        // fréquent : on tape un rotatif puis on ouvre le mode avancé — ses écouteurs étaient posés
        // sur un cadran qui n'existe plus, `pointerup` ne viendra jamais, et l'état de glisser
        // reste armé sur des nœuds détachés. On le solde AVANT de reconstruire.
        if(drag){
            try { drag.dial.releasePointerCapture && drag.dial.releasePointerCapture(); } catch(_){}
            try {
                drag.dial.removeEventListener('pointermove', knobMove);
                drag.dial.removeEventListener('pointerup', knobUp);
                drag.dial.removeEventListener('pointercancel', knobUp);
            } catch(_){}
            drag=null;
        }
        EL.innerHTML=`<div id="cc-state-bar" class="cc-state-bar"></div><div id="cc-error-slot"></div>
            <div class="cc-toolbar">${saveBlock}</div>
            <div class="cc-section"><h4>Réglages de base</h4>${basicHtml}</div>
            ${advToggle}${advHtml}
            <p class="cc-hint">Glisser le bouton vers le haut / bas, molette ou flèches clavier. Double-clic = réinitialiser.</p>`;
        updateStateBar(); renderError();
        if(saveOpen){ const inp=$('#cc-preset-name'); if(inp) inp.focus(); }
    }

    function toggleAdvanced(on){ advanced=on; render(); }
    function setGlow(on){
        // MAJ optimiste de l'état local + re-render → la bascule reflète immédiatement (sans
        // attendre le poll 3 s ni recharger la page), et la zone des réglages glow se grise/dégrise.
        if(state&&state.params){ state.params.glow_enabled = on?1:0; }
        postParams({glow_enabled: on?1:0});
        render();
    }
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
        knobNumberInput, knobNumberCommit, toggleAdvanced, setGlow, cancelPending,
        loadPreset, savePresetOpen, savePresetCancel, savePresetConfirm,
        deletePresetAsk, deletePresetConfirm, resetAllAsk, resetAllConfirm};
    window.__cc = exp;   // raccourci pour les handlers inline du fragment
    return exp;
})();
