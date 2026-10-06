(() => {
  const modal = document.querySelector('#loading-modal');
  const title = document.querySelector('#loading-title');
  const copy = document.querySelector('#loading-copy');

  document.querySelectorAll('[data-table-filter]').forEach((input) => {
    input.addEventListener('input', () => {
      const table = document.querySelector(`#${input.dataset.tableFilter}`);
      const term = input.value.trim().toLowerCase();
      table?.querySelectorAll('tbody tr').forEach((row) => {
        row.hidden = term && !row.textContent.toLowerCase().includes(term);
      });
    });
  });

  document.querySelectorAll('.analysis-form').forEach((form) => {
    form.addEventListener('submit', async (event) => {
      const start = form.elements.start_datetime.value;
      const end = form.elements.end_datetime.value;
      if (start && end && new Date(start) >= new Date(end)) {
        event.preventDefault();
        window.alert('The start date must be before the end date.');
        return;
      }
      event.preventDefault();
      modal.hidden = false;
      title.textContent = 'Reading Git history';
      copy.textContent = 'Calculating change, churn, and author ownership. This can take a moment.';
      try {
        const response = await fetch(form.action, {
          method: 'POST', body: new FormData(form), headers: { Accept: 'application/json' },
        });
        const result = await response.json();
        if (!response.ok) throw new Error(result.error || 'Analysis could not be completed.');
        window.location.assign(result.redirect);
      } catch (error) {
        title.textContent = 'Analysis could not start';
        copy.textContent = error.message;
        setTimeout(() => { modal.hidden = true; }, 3500);
      }
    });
  });
})();
