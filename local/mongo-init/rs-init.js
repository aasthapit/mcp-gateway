// Initiate the single-node replica set `rs0`. Idempotent: no-op when already initiated.
// Member host is localhost:27017 so host-side clients can connect with
//   mongodb://root:devpass@localhost:27017/?replicaSet=rs0&directConnection=true
// and in-network clients with mongo:27017 + directConnection=true.
(() => {
  const member = "localhost:27017";
  let status;
  try {
    status = rs.status();
  } catch (e) {
    if (e.codeName !== "NotYetInitialized") throw e;
  }
  if (status && status.ok === 1) {
    print(`replica set ${status.set} already initiated (${status.members[0].stateStr})`);
  } else {
    const res = rs.initiate({ _id: "rs0", members: [{ _id: 0, host: member }] });
    if (res.ok !== 1) throw new Error(`rs.initiate failed: ${JSON.stringify(res)}`);
    print(`rs.initiate ok (${member})`);
  }
  // Wait until this node is PRIMARY so that the next scripts can write.
  for (let i = 0; i < 60; i++) {
    const hello = db.hello();
    if (hello.isWritablePrimary) {
      print("node is PRIMARY");
      return;
    }
    sleep(500);
  }
  throw new Error("timed out waiting for PRIMARY");
})();
