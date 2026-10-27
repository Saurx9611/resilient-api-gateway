// Package breaker implements an in-process, per-upstream circuit breaker.
package breaker

import (
	"errors"
	"sync"
	"time"
)

type State string

const (
	Closed   State = "closed"
	Open     State = "open"
	HalfOpen State = "half_open"
)

type Outcome uint8

const (
	Neutral Outcome = iota
	Success
	Failure
)

type Policy struct {
	FailureThreshold  int
	OpenDuration      time.Duration
	MaxProbes         int
	RecoverySuccesses int
}

func DefaultPolicy() Policy { return Policy{5, 5 * time.Second, 1, 2} }
func (p Policy) Validate() error {
	if p.FailureThreshold < 1 || p.FailureThreshold > 10000 || p.OpenDuration < time.Millisecond || p.OpenDuration > time.Hour || p.MaxProbes < 1 || p.MaxProbes > 64 || p.RecoverySuccesses < 1 || p.RecoverySuccesses > 10000 {
		return errors.New("invalid circuit policy: threshold/recovery 1..10000, open 1ms..1h, probes 1..64")
	}
	return nil
}

// Snapshot is a consistent metrics view. Transitions are indexed Closed, Open,
// HalfOpen (rows=from, columns=to); labels must be fixed configuration IDs.
type Snapshot struct {
	State                                                State
	ConsecutiveFailures, RecoverySuccesses, ActiveProbes int
	Rejected, Failures, Successes, Neutral               uint64
	Transitions                                          [3][3]uint64
}
type Breaker struct {
	mu       sync.Mutex
	policy   Policy
	now      func() time.Time
	state    State
	epoch    uint64
	until    time.Time
	snapshot Snapshot
}

// New's clock must be concurrency-safe and advance monotonically. Nil uses
// time.Now, retaining its monotonic clock; it is never converted to Unix time.
func New(p Policy, now func() time.Time) (*Breaker, error) {
	if err := p.Validate(); err != nil {
		return nil, err
	}
	if now == nil {
		now = time.Now
	}
	return &Breaker{policy: p, now: now, state: Closed}, nil
}
func index(s State) int {
	switch s {
	case Open:
		return 1
	case HalfOpen:
		return 2
	}
	return 0
}
func (b *Breaker) transition(s State) {
	b.snapshot.Transitions[index(b.state)][index(s)]++
	b.state = s
	b.epoch++
	b.snapshot.ConsecutiveFailures = 0
	b.snapshot.RecoverySuccesses = 0
	if s == Open {
		b.until = b.now().Add(b.policy.OpenDuration)
	}
}
func (b *Breaker) Snapshot() Snapshot {
	b.mu.Lock()
	defer b.mu.Unlock()
	s := b.snapshot
	s.State = b.state
	return s
}

// Permit must be completed even when work is abandoned. Complete is idempotent.
type Permit struct {
	b     *Breaker
	epoch uint64
	probe bool
	once  sync.Once
}

// Acquire rejects without waiting. Retry is advisory (a probe may still be busy).
func (b *Breaker) Acquire() (*Permit, time.Duration) {
	b.mu.Lock()
	defer b.mu.Unlock()
	if b.state == Open {
		remaining := b.until.Sub(b.now())
		if remaining > 0 {
			b.snapshot.Rejected++
			return nil, remaining
		}
		b.transition(HalfOpen)
	}
	probe := b.state == HalfOpen
	if probe && (b.snapshot.ActiveProbes >= b.policy.MaxProbes || b.snapshot.RecoverySuccesses+b.snapshot.ActiveProbes >= b.policy.RecoverySuccesses) {
		b.snapshot.Rejected++
		return nil, time.Second
	}
	if probe {
		b.snapshot.ActiveProbes++
	}
	return &Permit{b: b, epoch: b.epoch, probe: probe}, 0
}
func (p *Permit) Complete(outcome Outcome) {
	p.once.Do(func() {
		b := p.b
		b.mu.Lock()
		defer b.mu.Unlock()
		if p.probe {
			b.snapshot.ActiveProbes--
		}
		if p.epoch != b.epoch {
			return
		}
		switch outcome {
		case Failure:
			b.snapshot.Failures++
		case Success:
			b.snapshot.Successes++
		default:
			b.snapshot.Neutral++
		}
		if b.state == Closed {
			if outcome == Success {
				b.snapshot.ConsecutiveFailures = 0
			}
			if outcome == Failure {
				b.snapshot.ConsecutiveFailures++
				if b.snapshot.ConsecutiveFailures >= b.policy.FailureThreshold {
					b.transition(Open)
				}
			}
		} else if b.state == HalfOpen {
			if outcome == Failure {
				b.transition(Open)
				return
			}
			if outcome == Success {
				b.snapshot.RecoverySuccesses++
			}
			if b.snapshot.RecoverySuccesses >= b.policy.RecoverySuccesses && b.snapshot.ActiveProbes == 0 {
				b.transition(Closed)
			}
		}
	})
}
