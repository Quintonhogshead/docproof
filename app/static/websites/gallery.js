(() => {
  const shell = document.documentElement;
  const configuredAuthor = shell.dataset.authorName || '';
  const authorName = configuredAuthor && !configuredAuthor.startsWith('__DOCPROOF_') ? configuredAuthor : 'Author';
  const templateButtons = [...document.querySelectorAll('[data-template]')];
  const configuredTemplate = shell.dataset.defaultTemplate || '';
  const defaultTemplate = templateButtons.some(button => button.dataset.template === configuredTemplate) ? configuredTemplate : 'literary-journal';
  const state = {template: defaultTemplate, page: 'home', screen: 'desktop'};
  const frame = document.querySelector('#preview'), device = document.querySelector('#device');
  const path = () => `./${state.template}/${state.page}/index.html`;
  document.querySelector('#galleryAuthor').textContent = authorName === 'Author' ? 'Author Website Preview' : `${authorName} / Website Preview`;
  document.querySelector('#introHeading').textContent = authorName === 'Author' ? 'Choose a direction for this author’s website.' : `Choose a direction for ${authorName}’s website.`;
  document.title = authorName === 'Author' ? 'Author Website Preview — DocProof' : `${authorName} — Website Preview | DocProof`;
  frame.title = authorName === 'Author' ? 'Selected author website preview' : `${authorName} website preview`;
  function show() { const url = path(); frame.src = url; device.className = `device ${state.screen}`; document.querySelector('#previewPath').textContent = `${state.template} / ${state.page}`; document.querySelector('#openPage').href = url; }
  templateButtons.forEach(button => button.addEventListener('click', () => { state.template = button.dataset.template; templateButtons.forEach(x => { const selected = x === button; x.classList.toggle('active', selected); x.setAttribute('aria-selected', String(selected)); }); show(); }));
  document.querySelectorAll('[data-page]').forEach(button => button.addEventListener('click', () => { state.page = button.dataset.page; document.querySelectorAll('[data-page]').forEach(x => x.classList.toggle('active', x === button)); show(); }));
  document.querySelectorAll('[data-screen]').forEach(button => button.addEventListener('click', () => { state.screen = button.dataset.screen; document.querySelectorAll('[data-screen]').forEach(x => x.classList.toggle('active', x === button)); show(); }));
  templateButtons.forEach(button => { const selected = button.dataset.template === state.template; button.classList.toggle('active', selected); button.setAttribute('aria-selected', String(selected)); });
  show();
})();
