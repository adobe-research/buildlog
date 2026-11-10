/*
 * SPDX-FileCopyrightText: Copyright 2026 Adobe. All rights reserved.
 * SPDX-License-Identifier: Apache-2.0
 */

#pragma once

#include <QObject>

// A QObject with a property, a signal and a slot: moc writes its
// meta-object (moc_counter.cpp), without which the link fails
// (Counter's vtable and staticMetaObject are in moc's output).
class Counter : public QObject {
  Q_OBJECT
  Q_PROPERTY(int value READ value WRITE setValue NOTIFY valueChanged)

public:
  explicit Counter(QObject *parent = nullptr);
  int value() const { return value_; }

public slots:
  void setValue(int value);
  void increment() { setValue(value_ + 1); }

signals:
  void valueChanged(int value);

private:
  int value_ = 0;
};
