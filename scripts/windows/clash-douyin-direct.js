// Clash Verge Rev 扩展脚本：固定抖音相关域名走 IPv4 直连。
// 仅修改列出的域名，保留其他代理、分组、规则和 DNS 策略。
function main(config) {
  const domains = [
    "douyin.com", "iesdouyin.com", "amemv.com",
    "douyinvod.com", "douyinpic.com", "douyinstatic.com",
  ];
  const name = "Douyin-Direct-IPv4";
  const rules = domains.map(domain => `DOMAIN-SUFFIX,${domain},${name}`);
  config.proxies = [
    { name, type: "direct", "ip-version": "ipv4", udp: true },
    ...(config.proxies || []).filter(proxy => proxy.name !== name),
  ];
  config.rules = [...rules, ...(config.rules || []).filter(rule => !rules.includes(rule))];
  config.dns = config.dns || {};
  config.dns["nameserver-policy"] = config.dns["nameserver-policy"] || {};
  for (const domain of domains) {
    config.dns["nameserver-policy"][`+.${domain}`] = "https://dns.alidns.com/dns-query";
  }
  return config;
}
