/* Website Studio deliberately uses only the documented API contract. */
(() => {
  const API = '/api/websites';
  const $ = (q, root = document) => root.querySelector(q);
  const state = { catalog: null, project: null, revision: null, page: 'home', viewport: 'desktop' };
  const formFields = [
    ['public_name','Public name'],['bio','Public biography'],['goal','Primary visitor action'],['target_reader','Intended reader'],['tone','Desired tone'],['avoid','Avoid'],
    ['book_title','Featured book title'],['book_subtitle','Subtitle'],['book_description','Approved book description'],['publication_date','Publication date'],['retailer_url','Book link'],
    ['contact_email','Public contact email'],['contact_url','Contact destination'],['excerpt','Approved excerpt'],['private_notes','Private constraints'],['cover_asset_id','Cover asset ID'],['portrait_asset_id','Portrait asset ID']
  ];
  const esc = (s) => String(s ?? '').replace(/[&<>"']/g, c => ({'&':'&amp;','<':'&lt;','>':'&gt;','"':'&quot;',"'":'&#39;'}[c]));
  async function request(path, options = {}) {
    const response = await fetch(`${API}${path}`, { headers: {'Content-Type':'application/json', ...(options.headers || {})}, ...options });
    if (!response.ok) { let detail = ''; try { detail = (await response.json()).detail || ''; } catch (_) {} throw new Error(detail || `Request failed (${response.status})`); }
    return response.status === 204 ? null : response.json();
  }
  function message(text = '', error = false) { const el = $('#specMessage'); if (el) { el.textContent = text; el.style.color = error ? 'var(--red)' : '#228252'; } }
  function renderList() {
    const list = $('#projectList'); const projects = state.catalog?.projects || [];
    list.innerHTML = projects.length ? projects.map(p => `<button class="project-row ${state.project?.project?.id === p.id ? 'active':''}" data-project="${esc(p.id)}"><strong>${esc(p.title || 'Untitled site')}</strong><span>${esc(p.state || 'waiting_for_inputs')} · ${esc(p.updated_at || '')}</span></button>`).join('') : '<p class="quiet">No websites yet.</p>';
    list.querySelectorAll('[data-project]').forEach(b => b.onclick = () => loadProject(b.dataset.project));
  }
  async function loadCatalog() {
    try { state.catalog = await request(''); $('#connection').textContent = state.catalog.web ? 'Connected' : 'Local studio'; renderList(); }
    catch (e) { $('#connection').textContent = 'Studio unavailable'; $('#projectList').innerHTML = `<p class="quiet">${esc(e.message)}</p>`; }
  }
  async function loadProject(id) {
    try { state.project = await request(`/projects/${encodeURIComponent(id)}`); state.revision = state.project.draft || state.project.revisions?.[0] || null; renderList(); renderProject(); }
    catch (e) { alert(e.message); }
  }
  function renderProject() {
    const root = $('#workspace'), tpl = $('#studioTemplate'); root.replaceChildren(tpl.content.cloneNode(true));
    const {project, questionnaire = {}, assets = [], revisions = [], settings = {}} = state.project;
    $('#projectState').textContent = project.state || 'Waiting for inputs'; $('#projectTitle').textContent = project.title || 'Untitled site';
    $('#projectMeta').textContent = `Author ${project.author_id || 'unassigned'} · ${revisions.length} revision${revisions.length === 1 ? '' : 's'}`;
    const models = state.catalog?.models || []; $('#modelPicker').innerHTML = models.map(m => `<option value="${esc(m.id)}" ${m.id === project.model ? 'selected':''}>${esc(m.display || m.id)}</option>`).join('') || '<option value="">No configured models</option>';
    $('#autoGenerate').checked = !!project.auto_generate;
    $('#templatePicker').innerHTML = (state.catalog?.templates || []).map(t => `<option value="${esc(t.id)}">${esc(t.name || t.id)}</option>`).join('');
    $('#templatePicker').value = state.revision?.spec?.template_id || questionnaire.template_id || 'literary-journal';
    const admin = !!state.catalog?.can_admin;
    $('#projectSetup').hidden = !admin;
    if (admin) fillSetup(settings, project);
    $('#readiness').innerHTML = readinessHtml(project, questionnaire, assets);
    $('#assetList').innerHTML = assets.length ? assets.map(a => `<div class="asset-row" data-asset-row="${esc(a.id)}"><div><strong>${esc(a.filename || a.id)}</strong><br><small>${esc(a.media_type || '')}</small></div><label>Description<input data-asset-alt value="${esc(a.alt || '')}" placeholder="Useful image description"></label><label class="switch"><input data-asset-approved type="checkbox" ${a.approved ? 'checked':''}> Public</label><label>Focus X<input data-asset-x type="number" min="0" max="1" step="0.01" value="${esc(a.focal_x ?? .5)}"></label><label>Focus Y<input data-asset-y type="number" min="0" max="1" step="0.01" value="${esc(a.focal_y ?? .5)}"></label><button data-save-asset="${esc(a.id)}">Save crop</button></div>`).join('') : '<p class="quiet">No assets uploaded.</p>';
    renderQuestionnaire(questionnaire); renderRevision(); renderRichEditor(); renderFindings(); renderHistory(revisions); bindStudio();
  }
  function readinessHtml(p, q, assets) {
    const checks = [['Identity',q.public_name],['Book',q.book_title],['Biography',q.bio],['Goal',q.goal],['Contact',q.contact_email || q.contact_url],['Cover',q.cover_asset_id || q.allow_cover_fallback || assets.some(a => a.approved)]];
    return checks.map(([name, value]) => `<div class="finding ${value ? 'ok':''}"><strong>${value ? 'Ready' : 'Needs input'}:</strong> ${name}</div>`).join('') + (p.error ? `<div class="finding error">${esc(p.error)}</div>` : '');
  }
  function renderQuestionnaire(q) {
    $('#staffQuestionnaire').innerHTML = formFields.map(([key,label]) => `<label>${label}<input data-question="${key}" value="${esc(q[key] || '')}"></label>`).join('') + `<label>Design<select data-question="template_id"><option value="auto">Choose for me</option>${(state.catalog?.templates || []).map(t => `<option value="${esc(t.id)}" ${q.template_id === t.id ? 'selected':''}>${esc(t.name || t.id)}</option>`).join('')}</select></label><label class="switch"><input data-question="allow_cover_fallback" type="checkbox" ${q.allow_cover_fallback ? 'checked':''}> Permit typographic cover fallback</label><label class="switch"><input data-question="rights_confirmed" type="checkbox" ${q.rights_confirmed ? 'checked':''}> Rights confirmed</label>`;
  }
  function fillSetup(settings, project) {
    const source = project.source_config || settings.source_config || {}, map = source.property_map || {};
    $('#sourceObjectType').value = source.object_type || '0-970'; $('#readyProperty').value = source.ready_property || '';
    $('#readyValue').value = source.ready_value || ''; $('#websiteUrlProperty').value = source.website_url_property || 'website_url';
    $('#websiteStatusProperty').value = source.website_status_property || ''; $('#liveValue').value = source.live_value || '';
    $('#driveFolderId').value = source.drive_folder_id || ''; $('#manuscriptFileId').value = source.manuscript_file_id || '';
    $('#coverFileId').value = source.cover_file_id || ''; $('#portraitFileId').value = source.portrait_file_id || '';
    $('#propertyMap').value = JSON.stringify(map, null, 2);
    const d = project.destination || {}; $('#destinationUrl').value = d.url || ''; $('#destinationSiteId').value = d.site_id || '';
    $('#destinationUsername').value = d.username || ''; $('#destinationPasswordEnv').value = d.password_env || '';
  }
  function setupPayload() {
    let property_map; try { property_map = JSON.parse($('#propertyMap').value || '{}'); } catch (_) { throw new Error('Property map must be valid JSON.'); }
    return {source_config:{object_type:$('#sourceObjectType').value.trim() || '0-970',property_map,ready_property:$('#readyProperty').value.trim(),ready_value:$('#readyValue').value.trim(),website_url_property:$('#websiteUrlProperty').value.trim() || 'website_url',website_status_property:$('#websiteStatusProperty').value.trim(),live_value:$('#liveValue').value.trim(),drive_folder_id:$('#driveFolderId').value.trim(),manuscript_file_id:$('#manuscriptFileId').value.trim(),cover_file_id:$('#coverFileId').value.trim(),portrait_file_id:$('#portraitFileId').value.trim()},destination:{url:$('#destinationUrl').value.trim(),site_id:$('#destinationSiteId').value.trim(),username:$('#destinationUsername').value.trim(),password_env:$('#destinationPasswordEnv').value.trim()}};
  }
  function readQuestionnaire() { const a = {...(state.project.questionnaire || {})}; $('#staffQuestionnaire').querySelectorAll('[data-question]').forEach(el => { a[el.dataset.question] = el.type === 'checkbox' ? el.checked : el.value.trim(); }); return a; }
  function renderRevision() { const r = state.revision; $('#revisionName').textContent = r ? `Revision ${r.id}` : 'No draft yet'; $('#specEditor').value = r ? JSON.stringify(r.spec || {}, null, 2) : ''; }
  function renderRichEditor() {
    const spec = state.revision?.spec || {}, author = spec.author || {}, contact = spec.contact || {}, cta = spec.primary_cta || {};
    const field = (key, label, value, multiline = false) => `<label ${multiline ? 'class="full"' : ''}>${label}${multiline ? `<textarea data-rich="${key}">${esc(value || '')}</textarea>` : `<input data-rich="${key}" value="${esc(value || '')}">`}</label>`;
    $('#richEditor').innerHTML = field('author.name','Author name',author.name) + field('headline','Homepage headline',spec.headline) + field('intro','Homepage introduction',spec.intro,true) + field('author.bio_short','Short biography',author.bio_short,true) + field('author.bio','Full biography',author.bio,true) + field('primary_cta.label','Primary button label',cta.label) + field('primary_cta.url','Primary button URL',cta.url) + field('contact.headline','Contact heading',contact.headline) + field('contact.message','Contact message',contact.message,true) + field('contact.email','Public contact email',contact.email) + field('contact.links','Contact links (label | URL, one per line)',(contact.links || []).map(x => `${x.label || ''} | ${x.url || ''}`).join('\n'),true) + field('praise','Praise (quote — attribution, one per line)',(spec.praise || []).map(x => `${x.quote || ''} — ${x.attribution || ''}`).join('\n'),true) + field('excerpt','Approved excerpt',spec.excerpt,true);
    const books = spec.books || [];
    $('#richBooks').innerHTML = books.length ? `<p class="eyebrow">Books</p>${books.map((b,i) => `<section class="book-edit"><h3>Book ${i + 1}</h3>${field(`books.${i}.title`,'Title',b.title)}${field(`books.${i}.subtitle`,'Subtitle',b.subtitle)}${field(`books.${i}.publication_date`,'Publication date',b.publication_date)}${field(`books.${i}.hook`,'Short hook',b.hook,true)}${field(`books.${i}.description`,'Description',b.description,true)}${field(`books.${i}.links`,'Book links (label | URL, one per line)',(b.links || []).map(x => `${x.label || ''} | ${x.url || ''}`).join('\n'),true)}</section>`).join('')}` : '<p class="quiet">This draft does not include any books.</p>';
  }
  function parseLinks(value) { return String(value || '').split('\n').map(x => x.trim()).filter(Boolean).map(line => { const parts = line.split('|').map(x => x.trim()); return {label:parts.shift() || 'Learn more',url:parts.join('|')}; }).filter(x => x.url); }
  function richSpec() {
    const spec = JSON.parse(JSON.stringify(state.revision?.spec || {}));
    $$('[data-rich]').forEach(el => {
      const path = el.dataset.rich, parts = path.split('.'); let parent = spec;
      while (parts.length > 1) { const key = parts.shift(); parent[key] ??= /^\d+$/.test(parts[0]) ? [] : {}; parent = parent[key]; }
      const key = parts[0], value = el.value.trim();
      if (path === 'contact.links' || path.endsWith('.links')) parent[key] = parseLinks(value);
      else if (path === 'praise') parent[key] = value.split('\n').map(x => x.trim()).filter(Boolean).map(line => { const point = line.lastIndexOf('—'); return point >= 0 ? {quote:line.slice(0,point).trim(),attribution:line.slice(point + 1).trim()} : {quote:line,attribution:''}; });
      else parent[key] = value;
    });
    return spec;
  }
  function renderFindings() { const findings = state.revision?.validation?.findings || []; $('#findings').innerHTML = findings.length ? findings.map((f,i) => `<div class="finding ${esc(f.severity || '')}"><strong>${esc(f.code || 'Check')}</strong><br>${esc(f.message || '')}<small>${esc(f.path || '')}</small>${String(f.severity || '').toLowerCase() === 'warning' ? `<label class="switch"><input type="checkbox" data-ack-finding="${esc(f.code || i)}"> I reviewed and accept this warning.</label>` : ''}</div>`).join('') : '<div class="finding ok">No reported findings for this revision.</div>'; }
  function renderHistory(revisions) { $('#history').innerHTML = revisions.length ? revisions.map(r => `<button class="history-row" data-revision="${esc(r.id)}"><span><strong>${esc(r.id)}</strong><br><small>${esc(r.created_at || '')}</small></span><span>${esc(r.validation?.status || 'pending')}${r.approved_at ? ' · approved' : ''}</span></button>`).join('') : '<p class="quiet">Draft revisions will appear here.</p>'; $('#history').querySelectorAll('[data-revision]').forEach(b => b.onclick = () => { state.revision = state.project.revisions.find(r => r.id === b.dataset.revision); renderProject(); });
    $('#rollbackPicker').innerHTML = revisions.filter(r => r.approved_at).map(r => `<option value="${esc(r.id)}">${esc(r.id)}${r.id === state.project.project.live_revision_id ? ' (live)' : ''}</option>`).join('') || '<option>No prior approved releases</option>';
  }
  function bindStudio() {
    $('[data-new-project]')?.addEventListener('click', openProjectDialog); $('#newProject').onclick = openProjectDialog;
    $$('.tabs button').forEach(b => b.onclick = () => switchTab(b.dataset.tab));
    $$('.head-actions button,[data-action]').forEach(b => b.onclick = () => action(b.dataset.action));
    $$('#assetList [data-save-asset]').forEach(b => b.onclick = () => saveAsset(b));
    $('#pagePicker').querySelectorAll('button').forEach(b => b.onclick = () => { state.page = b.dataset.page; setSelected('#pagePicker', b); loadPreview(); });
    $('#viewportPicker').querySelectorAll('button').forEach(b => b.onclick = () => { state.viewport = b.dataset.viewport; setSelected('#viewportPicker', b); $('#previewStage').className = `preview-stage ${state.viewport}`; });
  }
  const $$ = (q, root = document) => [...root.querySelectorAll(q)];
  function setSelected(root, button) { $$("button", $(root)).forEach(x => x.classList.toggle('active', x === button)); }
  function switchTab(name) { $$('.tabs button').forEach(b => b.classList.toggle('active', b.dataset.tab === name)); $$('[data-panel]').forEach(p => p.classList.toggle('hidden', p.dataset.panel !== name)); if (name === 'preview') loadPreview(); }
  function previewUrl() { const id = state.project.project.id, r = state.revision?.id; return r ? `${API}/projects/${encodeURIComponent(id)}/revisions/${encodeURIComponent(r)}/preview/${state.page}` : ''; }
  function loadPreview() { const url = previewUrl(); $('#previewFrame').src = url || 'about:blank'; }
  async function action(kind) {
    const p = state.project.project, id = encodeURIComponent(p.id), r = state.revision;
    try {
      if (kind === 'invite') { const x = await request(`/projects/${id}/invitations`, {method:'POST',body:'{}'}); await navigator.clipboard?.writeText(x.url); alert(`Questionnaire link copied:\n${x.url}`); }
      if (kind === 'collect') await request(`/projects/${id}/collect`, {method:'POST',body:'{}'});
      if (kind === 'generate') await request(`/projects/${id}/generate`, {method:'POST',body:JSON.stringify({model:$('#modelPicker').value})});
      if (kind === 'save-questionnaire') await request(`/projects/${id}/questionnaire`, {method:'PUT',body:JSON.stringify({answers:readQuestionnaire(),submit:false})});
      if (kind === 'save-setup') await request(`/projects/${id}`, {method:'PATCH',body:JSON.stringify(setupPayload())});
      if (kind === 'save-spec' || kind === 'switch-template' || kind === 'save-rich-spec') { let spec; try { spec = kind === 'save-rich-spec' ? richSpec() : JSON.parse($('#specEditor').value || '{}'); } catch (_) { return message('The public specification must be valid JSON.', true); } if (kind === 'switch-template') spec.template_id = $('#templatePicker').value; await request(`/projects/${id}/revisions`, {method:'POST',body:JSON.stringify({spec,expected_revision_id:r?.id || null})}); }
      if (kind === 'revise') await request(`/projects/${id}/revise`, {method:'POST',body:JSON.stringify({instructions:$('#revisionInstructions').value.trim(),expected_revision_id:r?.id || null,model:$('#modelPicker').value})});
      if (kind === 'stage') await request(`/projects/${id}/revisions/${encodeURIComponent(r.id)}/stage`, {method:'POST',body:'{}'});
      if (kind === 'approve') { const warnings = (r.validation?.findings || []).filter(f => String(f.severity || '').toLowerCase() === 'warning'); const codes = warnings.map((f,i) => f.code || String(i)); const checked = $$('[data-ack-finding]:checked').map(x => x.dataset.ackFinding); if (codes.some(code => !checked.includes(code))) return message('Acknowledge each warning before approving this revision.', true); await request(`/projects/${id}/revisions/${encodeURIComponent(r.id)}/approve`, {method:'POST',body:JSON.stringify({acknowledged_findings:checked,expected_revision_id:r.id})}); }
      if (kind === 'publish') await request(`/projects/${id}/revisions/${encodeURIComponent(r.id)}/publish`, {method:'POST',body:'{}'});
      if (kind === 'rollback') await request(`/projects/${id}/rollback`, {method:'POST',body:JSON.stringify({release_id:$('#rollbackPicker').value})});
      if (kind === 'stage' || kind === 'approve' || kind === 'publish' || kind === 'rollback') message('Request completed. Refreshing revision status…');
      await loadProject(p.id);
    } catch (e) { message(e.message, true); alert(e.message); }
  }
  function openProjectDialog() { $('#projectDialog').showModal(); }
  async function saveAsset(button) { const row = button.closest('[data-asset-row]'), id = encodeURIComponent(button.dataset.saveAsset); try { await request(`/projects/${encodeURIComponent(state.project.project.id)}/assets/${id}`, {method:'PATCH',body:JSON.stringify({alt:$('[data-asset-alt]',row).value.trim(),approved:$('[data-asset-approved]',row).checked,focal_x:Number($('[data-asset-x]',row).value),focal_y:Number($('[data-asset-y]',row).value)})}); await loadProject(state.project.project.id); } catch (e) { alert(e.message); } }
  $('#projectDialog').addEventListener('click', e => { if (e.target === $('#projectDialog') || e.target.matches('[data-close]')) $('#projectDialog').close(); });
  $('#projectForm').addEventListener('submit', async e => { e.preventDefault(); const data = Object.fromEntries(new FormData(e.currentTarget)); data.hubspot_project_ids = data.hubspot_project_ids.split(',').map(x => x.trim()).filter(Boolean); try { const p = await request('/projects',{method:'POST',body:JSON.stringify(data)}); $('#projectDialog').close(); await loadCatalog(); await loadProject(p.id); } catch (err) { $('#projectMessage').textContent = err.message; } });
  $('#studioSettings').onclick = async () => { try { const s = await request('/settings'); const f = $('#settingsForm'); f.elements.automation_enabled.checked = !!s.automation_enabled; f.elements.poll_seconds.value = s.poll_seconds ?? ''; f.elements.max_cost_usd.value = s.max_cost_usd ?? ''; f.elements.model_context_tokens.value = JSON.stringify(s.model_context_tokens || {}, null, 2); f.elements.staff_roles.value = JSON.stringify(s.staff_roles || {}, null, 2); $('#settingsDialog').showModal(); } catch (e) { alert(e.message); } };
  $('#settingsForm').addEventListener('submit', async e => { e.preventDefault(); const f = e.currentTarget; try { const payload = {automation_enabled:f.elements.automation_enabled.checked,poll_seconds:Number(f.elements.poll_seconds.value),max_cost_usd:Number(f.elements.max_cost_usd.value),model_context_tokens:JSON.parse(f.elements.model_context_tokens.value || '{}'),staff_roles:JSON.parse(f.elements.staff_roles.value || '{}')}; await request('/settings',{method:'PUT',body:JSON.stringify(payload)}); $('#settingsDialog').close(); } catch (err) { $('#settingsMessage').textContent = `Check the settings: ${err.message}`; } });
  document.addEventListener('change', async e => {
    if (!state.project) return; const p = state.project.project, id = encodeURIComponent(p.id);
    try {
      if (e.target.id === 'autoGenerate') await request(`/projects/${id}`, {method:'PATCH',body:JSON.stringify({auto_generate:e.target.checked})});
      if (e.target.id === 'manuscriptUpload') { const form = new FormData(); form.append('file', e.target.files[0]); const res = await fetch(`${API}/projects/${id}/manuscript`, {method:'POST',body:form}); if (!res.ok) throw new Error('Manuscript upload failed'); }
      if (e.target.id === 'assetUpload') { const form = new FormData(); form.append('file', e.target.files[0]); form.append('alt',$('#assetAlt').value); form.append('approved',$('#assetApproved').checked); const res = await fetch(`${API}/projects/${id}/assets`, {method:'POST',body:form}); if (!res.ok) throw new Error('Asset upload failed'); }
      if (['autoGenerate','manuscriptUpload','assetUpload'].includes(e.target.id)) await loadProject(p.id);
    } catch (err) { alert(err.message); }
  });
  function renderReleaseControls() { /* retained as named extension point for route-driven status refresh */ }
  const originalRender = renderProject;
  renderProject = function () { originalRender(); const r = state.revision, c = $('#releaseControls'); if (!c) return; if (!r) { c.innerHTML = '<p class="quiet">Generate a draft before release actions.</p>'; return; } const staged = !!r.staged_preview_url; c.innerHTML = `<p class="quiet">1. Stage this exact revision. 2. Open and inspect the real WordPress preview. 3. Acknowledge any warnings and approve. 4. Publish.</p><button data-action="stage">${staged ? 'Restage private WordPress preview' : '1. Stage private WordPress preview'}</button>${staged ? `<a href="${esc(r.staged_preview_url)}" target="_blank" rel="noopener">2. Open staged preview to inspect</a><button data-action="approve" class="primary">3. Approve this exact revision</button>` : '<p class="quiet">Approval becomes available after staging.</p>'}${r.approved_at ? '<button data-action="publish" class="primary">4. Publish approved revision</button>' : ''}`; $$("[data-action]",c).forEach(b => b.onclick=()=>action(b.dataset.action)); };
  loadCatalog().then(() => { if (state.catalog?.can_admin) $('#studioSettings').hidden = false; });
})();
