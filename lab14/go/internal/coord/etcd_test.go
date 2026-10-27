package coord

import (
	"context"
	"fmt"
	"io"
	"log/slog"
	"net"
	"net/url"
	"strconv"
	"testing"
	"time"

	clientv3 "go.etcd.io/etcd/client/v3"
	"go.etcd.io/etcd/server/v3/embed"
)

// freePort берёт случайный свободный порт (слушатель сразу закрывается).
func freePort(t *testing.T) int {
	t.Helper()
	l, err := net.Listen("tcp", "127.0.0.1:0")
	if err != nil {
		t.Fatal(err)
	}
	defer l.Close()
	return l.Addr().(*net.TCPAddr).Port
}

// startEtcd поднимает встроенный etcd на случайных портах в t.TempDir().
func startEtcd(t *testing.T) string {
	t.Helper()
	cfg := embed.NewConfig()
	cfg.Dir = t.TempDir()
	cfg.LogLevel = "error"
	cport, pport := freePort(t), freePort(t)
	cu, _ := url.Parse(fmt.Sprintf("http://127.0.0.1:%d", cport))
	pu, _ := url.Parse(fmt.Sprintf("http://127.0.0.1:%d", pport))
	cfg.ListenClientUrls = []url.URL{*cu}
	cfg.AdvertiseClientUrls = []url.URL{*cu}
	cfg.ListenPeerUrls = []url.URL{*pu}
	cfg.AdvertisePeerUrls = []url.URL{*pu}
	cfg.InitialCluster = cfg.Name + "=" + pu.String()

	e, err := embed.StartEtcd(cfg)
	if err != nil {
		t.Fatal(err)
	}
	t.Cleanup(e.Close)
	select {
	case <-e.Server.ReadyNotify():
	case <-time.After(20 * time.Second):
		t.Fatal("etcd не стартовал")
	}
	return "127.0.0.1:" + strconv.Itoa(cport)
}

func newClient(t *testing.T, ep string) *clientv3.Client {
	t.Helper()
	cli, err := clientv3.New(clientv3.Config{Endpoints: []string{ep}, DialTimeout: 5 * time.Second})
	if err != nil {
		t.Fatal(err)
	}
	t.Cleanup(func() { _ = cli.Close() })
	return cli
}

func quietLog() *slog.Logger { return slog.New(slog.NewTextHandler(io.Discard, nil)) }

func devs50(context.Context) ([]string, error) {
	out := make([]string, 50)
	for i := range out {
		out[i] = fmt.Sprintf("dev-%04d", i+1)
	}
	return out, nil
}

func dial(t *testing.T, ep, id string) *Assigner {
	t.Helper()
	a, err := Dial(context.Background(), Options{
		Endpoints: []string{ep}, TTL: 10 * time.Second, Self: Member{CollectorID: id,
			FlightAddr: "127.0.0.1:8815", StatusAddr: "127.0.0.1:9100", StartedAt: "2026-10-13T09:30:00Z"},
		Devices: devs50, Log: quietLog(), Now: time.Now,
	})
	if err != nil {
		t.Fatal(err)
	}
	return a
}

func eventually(t *testing.T, what string, cond func() bool) {
	t.Helper()
	deadline := time.Now().Add(15 * time.Second)
	for time.Now().Before(deadline) {
		if cond() {
			return
		}
		time.Sleep(50 * time.Millisecond)
	}
	t.Fatalf("таймаут: %s", what)
}

func TestMembershipUnderLease(t *testing.T) {
	ep := startEtcd(t)
	a := dial(t, ep, "c1")
	cli := newClient(t, ep)

	resp, err := cli.Get(context.Background(), "/lab14/collectors/c1")
	if err != nil {
		t.Fatal(err)
	}
	if len(resp.Kvs) != 1 {
		t.Fatalf("ключ членства отсутствует")
	}
	kv := resp.Kvs[0]
	if kv.Lease == 0 {
		t.Fatal("ключ членства не привязан к lease")
	}
	if clientv3.LeaseID(kv.Lease) != a.lease {
		t.Fatalf("lease ключа %d, ожидался lease сборщика %d", kv.Lease, a.lease)
	}
	ttl, err := cli.TimeToLive(context.Background(), clientv3.LeaseID(kv.Lease))
	if err != nil {
		t.Fatal(err)
	}
	if ttl.TTL <= 0 || ttl.TTL > 10 {
		t.Fatalf("TTL lease %d, ожидалось (0, 10]", ttl.TTL)
	}
	if err := a.Close(context.Background()); err != nil {
		t.Fatalf("Close: %v", err)
	}
}

func TestMemberKeyRemovedAfterClose(t *testing.T) {
	ep := startEtcd(t)
	a := dial(t, ep, "c1")
	cli := newClient(t, ep)

	if err := a.Close(context.Background()); err != nil {
		t.Fatalf("Close: %v", err)
	}
	resp, err := cli.Get(context.Background(), "/lab14/collectors/c1")
	if err != nil {
		t.Fatal(err)
	}
	if len(resp.Kvs) != 0 {
		t.Fatal("ключ членства не исчез после Close (lease не отозван)")
	}
}

func TestPeersSplitDevicesAndSeeEachOther(t *testing.T) {
	ep := startEtcd(t)
	a1 := dial(t, ep, "c1")
	defer a1.Close(context.Background())
	a2 := dial(t, ep, "c2")
	defer a2.Close(context.Background())

	eventually(t, "оба сборщика видят друг друга", func() bool {
		return len(a1.Members()) == 2 && len(a2.Members()) == 2
	})
	eventually(t, "устройства разделены без пересечений", func() bool {
		d1, _ := a1.Devices(context.Background())
		d2, _ := a2.Devices(context.Background())
		seen := map[string]bool{}
		for _, d := range d1 {
			seen[d] = true
		}
		for _, d := range d2 {
			if seen[d] {
				return false
			}
		}
		return len(d1)+len(d2) == 50
	})
}

func TestCheckpointNeverRollsBack(t *testing.T) {
	ep := startEtcd(t)
	a := dial(t, ep, "c1")
	defer a.Close(context.Background())
	ctx := context.Background()
	cps := a.Checkpoints()

	if err := cps.Save(ctx, map[string]int64{"dev-0001": 5}); err != nil {
		t.Fatal(err)
	}
	if err := cps.Save(ctx, map[string]int64{"dev-0001": 3}); err != nil {
		t.Fatal(err)
	}
	got, err := cps.Load(ctx, "dev-0001")
	if err != nil {
		t.Fatal(err)
	}
	if got != 5 {
		t.Fatalf("чекпоинт откатился: %d, ожидалось 5", got)
	}

	if err := cps.Save(ctx, map[string]int64{"dev-0001": 9}); err != nil {
		t.Fatal(err)
	}
	if got, _ := cps.Load(ctx, "dev-0001"); got != 9 {
		t.Fatalf("чекпоинт %d, ожидалось 9", got)
	}
}

// Старый владелец, узнав новое значение от другого сборщика, не должен откатить его,
// даже если его собственный floor меньше (проверка транзакцией в etcd).
func TestCheckpointStoreRejectsStaleWriterInEtcd(t *testing.T) {
	ep := startEtcd(t)
	newer := dial(t, ep, "c2")
	defer newer.Close(context.Background())
	stale := dial(t, ep, "c1")
	defer stale.Close(context.Background())
	ctx := context.Background()

	if err := newer.Checkpoints().Save(ctx, map[string]int64{"dev-0007": 40}); err != nil {
		t.Fatal(err)
	}
	if err := stale.Checkpoints().Save(ctx, map[string]int64{"dev-0007": 12}); err != nil {
		t.Fatal(err)
	}
	if got, _ := stale.Checkpoints().Load(ctx, "dev-0007"); got != 40 {
		t.Fatalf("чекпоинт откатился старым владельцем: %d, ожидалось 40", got)
	}
}
