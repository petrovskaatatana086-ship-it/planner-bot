const assert = require('node:assert/strict');
const fs = require('node:fs');
const vm = require('node:vm');
const source = fs.readFileSync('miniapp/live.js', 'utf8');
function helpers(zone, role='client', slots=[]) {
  const requests=[];
  const sandbox={URL,location:{href:'https://example.test/meetly/'},window:{},document:{},crypto:{},
    Intl:{DateTimeFormat:function(locale,options={}) {
      return new Intl.DateTimeFormat(locale,{timeZone:zone,...options});
    }},requests,fixture:slots};
  const prefix=source.slice(0,source.indexOf('  const line ='));
  vm.runInNewContext(prefix+`
    async function api(path){requests.push(path);const q=new URL('https://example.test/'+path).searchParams;
      return {settings_version:1,duration_minutes:60,slots:fixture.filter(s=>{const d=dayKey(s.start_at,'Europe/Moscow');return d>=q.get('from')&&d<=q.get('to');})};}
    state.me={role:${JSON.stringify(role)}};
    globalThis.h={timeLabel,dateLabel,dayKey,zoneLabel,timeRange,localAvailability,today};
  })();`,sandbox);
  return {h:sandbox.h,requests};
}
(async()=>{
  const instant='2026-10-28T12:00:00Z';
  for(const [zone,time,label] of [
    ['Europe/Moscow','15:00','МСК'],['Asia/Omsk','18:00','МСК +3 ч'],
    ['Europe/Berlin','13:00','МСК −2 ч'],['Asia/Kolkata','17:30','МСК +2 ч 30 мин'],
    ['Pacific/Marquesas','02:30','МСК −12 ч 30 мин']]){
    const {h}=helpers(zone);assert.equal(h.timeLabel(instant),time);assert.equal(h.zoneLabel(instant),label);
    assert.equal(h.dateLabel('2026-10-28'),'28 октября');
  }
  const berlin=helpers('Europe/Berlin').h;
  assert.equal(berlin.zoneLabel('2026-07-01T12:00:00Z'),'МСК −1 ч');
  assert.equal(berlin.zoneLabel('2026-12-01T12:00:00Z'),'МСК −2 ч');
  const admin=helpers('Asia/Omsk','owner').h;
  assert.equal(admin.timeLabel(instant),'15:00');assert.equal(admin.zoneLabel(instant),'МСК');
  const slot={start_at:'2026-12-31T20:00:00Z',end_at:'2026-12-31T21:00:00Z'};
  const {h,requests}=helpers('Pacific/Kiritimati','client',[slot]);
  assert.equal(h.dayKey(slot.start_at),'2027-01-01');
  const result=await h.localAvailability('2027-01-01','2027-01-31',null);
  assert.equal(result.slots.length,1);assert.equal(result.days[0].date,'2027-01-01');
  assert.equal(requests.length,2);
  for(const path of requests){const q=new URL('https://example.test/'+path).searchParams;
    assert.ok((Date.parse(q.get('to'))-Date.parse(q.get('from')))/86400000<=30);}
  const west=helpers('America/Adak','client',[{start_at:'2027-01-01T07:00:00Z',end_at:'2027-01-01T08:00:00Z'}]).h;
  assert.equal((await west.localAvailability('2026-12-31','2026-12-31',null)).slots.length,1);
  assert.match(helpers('Asia/Omsk').h.timeRange('2026-10-28T17:30:00Z','2026-10-28T18:30:00Z'),/29 октября/);
  console.log('Timezone checks passed: local times, offsets, DST, admin Moscow, date/year boundaries, API chunks.');
})().catch(e=>{console.error(e);process.exitCode=1;});
