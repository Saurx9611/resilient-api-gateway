package breaker

import (
	"sync"
	"sync/atomic"
	"testing"
	"time"
)

type clock struct {
	mu sync.Mutex
	t  time.Time
}

func (c *clock) now() time.Time          { c.mu.Lock(); defer c.mu.Unlock(); return c.t }
func (c *clock) advance(d time.Duration) { c.mu.Lock(); c.t = c.t.Add(d); c.mu.Unlock() }
func setup(t *testing.T, p Policy) (*Breaker, *clock) {
	t.Helper()
	c := &clock{t: time.Now()}
	b, e := New(p, c.now)
	if e != nil {
		t.Fatal(e)
	}
	return b, c
}
func take(t *testing.T, b *Breaker) *Permit {
	t.Helper()
	p, _ := b.Acquire()
	if p == nil {
		t.Fatal("unexpected rejection", b.Snapshot())
	}
	return p
}
func state(t *testing.T, b *Breaker, s State) {
	t.Helper()
	if got := b.Snapshot().State; got != s {
		t.Fatalf("state %s want %s", got, s)
	}
}

func TestTransitionsAndRecovery(t *testing.T) {
	b, c := setup(t, Policy{2, time.Second, 2, 3})
	take(t, b).Complete(Failure)
	take(t, b).Complete(Success)
	take(t, b).Complete(Failure)
	take(t, b).Complete(Neutral)
	state(t, b, Closed)
	stale := take(t, b)
	take(t, b).Complete(Failure)
	state(t, b, Open)
	stale.Complete(Success)
	stale.Complete(Failure)
	state(t, b, Open)
	if p, d := b.Acquire(); p != nil || d != time.Second {
		t.Fatalf("open retry %v %s", p, d)
	}
	c.advance(time.Second - time.Nanosecond)
	if p, _ := b.Acquire(); p != nil {
		t.Fatal("early probe")
	}
	c.advance(time.Nanosecond)
	p1, p2 := take(t, b), take(t, b)
	state(t, b, HalfOpen)
	if p, _ := b.Acquire(); p != nil {
		t.Fatal("probe cap")
	}
	p1.Complete(Success)
	p2.Complete(Neutral)
	state(t, b, HalfOpen)
	p1, p2 = take(t, b), take(t, b)
	p1.Complete(Success)
	state(t, b, HalfOpen)
	if p, _ := b.Acquire(); p != nil {
		t.Fatal("overadmitted recovery target")
	}
	p2.Complete(Success)
	state(t, b, Closed)
	s := b.Snapshot()
	if s.ActiveProbes != 0 || s.Transitions[0][1] != 1 || s.Transitions[1][2] != 1 || s.Transitions[2][0] != 1 {
		t.Fatal(s)
	}
}

func TestHalfOpenFailureAndStaleProbes(t *testing.T) {
	b, c := setup(t, Policy{1, time.Second, 2, 2})
	take(t, b).Complete(Failure)
	c.advance(time.Second)
	first, stale := take(t, b), take(t, b)
	first.Complete(Failure)
	state(t, b, Open)
	c.advance(time.Second)
	fresh := take(t, b)
	if p, _ := b.Acquire(); p != nil {
		t.Fatal("old probe escaped concurrency bound")
	}
	stale.Complete(Success)
	fresh.Complete(Success)
	state(t, b, HalfOpen)
	take(t, b).Complete(Success)
	state(t, b, Closed)
	if b.Snapshot().Transitions[2][1] != 1 {
		t.Fatal("missing reopen transition")
	}
}

func TestSimultaneousProbeAdmission(t *testing.T) {
	b, c := setup(t, Policy{1, time.Second, 3, 5})
	take(t, b).Complete(Failure)
	c.advance(time.Second)
	var wg sync.WaitGroup
	var admitted atomic.Int32
	permits := make(chan *Permit, 100)
	start := make(chan struct{})
	for i := 0; i < 100; i++ {
		wg.Add(1)
		go func() {
			defer wg.Done()
			<-start
			p, _ := b.Acquire()
			if p != nil {
				admitted.Add(1)
				permits <- p
			}
			b.Snapshot()
		}()
	}
	close(start)
	wg.Wait()
	close(permits)
	if admitted.Load() != 3 || b.Snapshot().ActiveProbes != 3 {
		t.Fatal("admission bound", admitted.Load(), b.Snapshot())
	}
	for p := range permits {
		wg.Add(1)
		go func(p *Permit) { defer wg.Done(); p.Complete(Success); p.Complete(Failure) }(p)
	}
	wg.Wait()
	state(t, b, HalfOpen)
	take(t, b).Complete(Success)
	take(t, b).Complete(Success)
	state(t, b, Closed)
	t.Log("100 simultaneous admissions: exactly 3 probes; duplicate completions ignored; recovered after 5 successes")
}

func TestPoliciesAndInstances(t *testing.T) {
	for _, p := range []Policy{{}, {1, 0, 1, 1}, {1, time.Second, 0, 1}, {1, time.Second, 1, 0}, {10001, time.Second, 1, 1}, {1, time.Hour + 1, 1, 1}, {1, time.Second, 65, 1}} {
		if _, e := New(p, nil); e == nil {
			t.Fatal("invalid policy", p)
		}
	}
	a, _ := setup(t, DefaultPolicy())
	b, _ := setup(t, DefaultPolicy())
	for i := 0; i < 5; i++ {
		take(t, a).Complete(Failure)
	}
	state(t, a, Open)
	state(t, b, Closed)
}
