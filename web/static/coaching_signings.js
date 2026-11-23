(() => {
  const panel = document.getElementById('coaching-signings');
  if (!panel) return;
  const boxes = [...panel.querySelectorAll('input[data-entry-id]')];
  const pending = document.getElementById('coaching-signings-pending');
  const status = document.getElementById('coaching-signings-save-status');
  const byId = new Map(boxes.map(box => [box.dataset.entryId, box]));

  function update() {
    let offered = 0, offers = 0, moved = 0, promotions = 0, visible = 0;
    boxes.forEach(box => {
      const row = box.closest('tr');
      if (row.dataset.action === 'offer') {
        offers += 1;
        if (box.checked) offered += 1;
      } else {
        promotions += 1;
        if (box.checked) moved += 1;
      }
      row.classList.toggle('coaching-offer-checked', box.checked);
      const missing = (row.dataset.dependsOn || '').split(',').filter(Boolean)
        .map(id => byId.get(id)).filter(required => required && !required.checked)
        .map(required => required.closest('tr').dataset.step);
      const warning = row.querySelector('.coaching-prerequisite');
      warning.textContent = missing.length ?
        `${box.checked ? 'Review: incomplete prerequisites —' : 'First complete'} step${missing.length > 1 ? 's' : ''} ${missing.join(', ')}. ` : '';
      row.classList.toggle('coaching-dependency-pending', missing.length > 0);
      row.hidden = pending.checked && box.checked;
      if (!row.hidden) visible += 1;
    });
    document.getElementById('coaching-signings-progress').textContent =
      `${offered} / ${offers} offers made · ${moved} / ${promotions} promotions completed`;
    const next = boxes.find(box => !box.checked);
    document.getElementById('coaching-signings-next').textContent = next ?
      `Next in order: ${next.getAttribute('aria-label').replace(': Promotion completed for', ': promote').replace(': Offer sent to', ': offer to')}` :
      'All actions tracked. Confirm external signings in OOTP; checked offers can still be pending.';
    document.getElementById('coaching-signings-empty').hidden = visible !== 0;
  }

  boxes.forEach(box => box.addEventListener('change', async () => {
    const checked = box.checked;
    box.disabled = true;
    status.classList.remove('save-error');
    status.textContent = 'Saving…';
    try {
      const response = await fetch(panel.dataset.endpoint, {
        method: 'POST',
        headers: {'Content-Type': 'application/json'},
        body: JSON.stringify({plan_id: panel.dataset.planId,
          entry_id: box.dataset.entryId, checked})
      });
      const result = await response.json();
      if (!response.ok || !result.ok) throw new Error(result.error || 'Unable to save');
      status.textContent = 'Saved';
    } catch (error) {
      box.checked = !checked;
      status.classList.add('save-error');
      status.textContent = 'Not saved. Checkbox reverted; please try again.';
    } finally {
      box.disabled = false;
      update();
    }
  }));
  pending.addEventListener('change', update);
  update();
})();
