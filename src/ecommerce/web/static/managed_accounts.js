(() => {
  window.updateManagedAccounts = (form, prefix) => {
    const data = document.getElementById(prefix + '-shop-data');
    const shops = JSON.parse(data.textContent);
    const field = document.getElementById(prefix + '-account-field');
    const old = form.elements.account_id;
    const select = old.cloneNode(false);
    const shop = shops.find(item => item.id === form.elements.shop_id.value);
    field.dispatchEvent(new Event('ui:controls-remove', {bubbles:true}));
    select.classList.remove('ui-searchable-native');
    select.disabled = !shop;
    select.dataset.placeholder = shop ? '选择千牛账号' : '先选择店铺';
    select.add(new Option(select.dataset.placeholder, ''));
    for (const unavailable of [false, true]) {
      const group = document.createElement('optgroup');
      group.label = unavailable ? '不可用账号' : '可用账号';
      for (const account of shop?.accounts || []) {
        if (Boolean(account.reason) !== unavailable) continue;
        const option = new Option(account.name + (account.reason ? ' · ' + account.reason : ''), account.id);
        option.disabled = unavailable;
        group.append(option);
      }
      if (group.children.length) select.append(group);
    }
    old.replaceWith(select);
    field.dispatchEvent(new Event('ui:controls-added', {bubbles:true}));
    document.getElementById(prefix + '-account-note').hidden = shops.length > 0 &&
      (!shop || shop.accounts.some(account => !account.reason));
    document.getElementById(prefix + '-account-note-text').textContent = shop ?
      '该店铺暂无可用千牛账号。' : '当前环境暂无已登记的千牛账号。';
  };
})();
