/*
* All or portions of this file Copyright (c) Amazon.com, Inc. or its affiliates or
* its licensors.
*
* For complete copyright and license terms please see the LICENSE at the root of this
* distribution (the "License"). All use of this software is governed by the License,
* or, if provided, by the license below or the license accompanying this file. Do not
* remove or modify any license notices. This file is distributed on an "AS IS" BASIS,
* WITHOUT WARRANTIES OR CONDITIONS OF ANY KIND, either express or implied.
*
*/
// Original file Copyright Crytek GMBH or its affiliates, used under license.

#include "stdafx.h"
#include "UiEditorAnimationBus.h"
#include "EditorDefs.h"
#include "UiAnimViewKeyPropertiesDlg.h"
#include "UiAnimViewDialog.h"
#include "UiAnimViewSequence.h"
#include "UiAnimViewTrack.h"
#include "UiAnimViewUndo.h"

#include <ISplines.h>
#include <QVBoxLayout>
#include <Animation/ui_UiAnimViewTrackPropsDlg.h>


//////////////////////////////////////////////////////////////////////////
void CUiAnimViewKeyUIControls::OnInternalVariableChange(IVariable* pVar)
{
    CUiAnimViewSequence* pSequence = nullptr;
    EBUS_EVENT_RESULT(pSequence, UiEditorAnimationBus, GetCurrentSequence);

    CUiAnimViewSequenceNotificationContext context(pSequence);
    CUiAnimViewKeyBundle keys = pSequence->GetSelectedKeys();

    bool bAlreadyRecording = UiAnimUndo::IsRecording();
    if (!bAlreadyRecording)
    {
        // Try to start undo. This can't be done if restoring undo
        UiAnimUndoManager::Get()->Begin();

        if (UiAnimUndo::IsRecording())
        {
            CUiAnimViewSequence* pSequence = nullptr;
            EBUS_EVENT_RESULT(pSequence, UiEditorAnimationBus, GetCurrentSequence);
            pSequence->StoreUndoForTracksWithSelectedKeys();
        }
        else
        {
            bAlreadyRecording = true;
        }
    }
    else
    {
        CUiAnimViewSequence* pSequence = nullptr;
        EBUS_EVENT_RESULT(pSequence, UiEditorAnimationBus, GetCurrentSequence);
        pSequence->StoreUndoForTracksWithSelectedKeys();
    }

    OnUIChange(pVar, keys);

    if (!bAlreadyRecording)
    {
        UiAnimUndoManager::Get()->Accept("Change Keys");
    }
}

//////////////////////////////////////////////////////////////////////////

CUiAnimViewKeyPropertiesDlg::CUiAnimViewKeyPropertiesDlg(QWidget* hParentWnd)
    : QWidget(hParentWnd)
    , m_pLastTrackSelected(NULL)
{
    QVBoxLayout* l = new QVBoxLayout();
    l->setMargin(0);
    m_wndTrackProps = new CUiAnimViewTrackPropsDlg(this);
    l->addWidget(m_wndTrackProps);
#if UI_ANIMATION_REMOVED    // UI_ANIMATION_REVISIT, do we want to support these props?
    m_wndProps = new ReflectedPropertyControl(this);
    m_wndProps->Setup();
    m_wndProps->setSizePolicy(QSizePolicy::Preferred, QSizePolicy::Expanding);
    l->addWidget(m_wndProps);
    m_wndProps->SetStoreUndoByItems(false);
#endif

    setLayout(l);

    m_pVarBlock = new CVarBlock;

#if UI_ANIMATION_REMOVED     // this ends up crashing - probably with DLL memory allocator issues
    // maybe getting a pointer back from Editor and using it is local to DLL
    // Add key UI classes
    std::vector<IClassDesc*> classes;
    GetIEditor()->GetClassFactory()->GetClassesBySystemID(ESYSTEM_CLASS_TRACKVIEW_KEYUI, classes);
    for (IClassDesc* iclass : classes)
    {
        if (QObject* pObj = iclass->CreateQObject())
        {
            auto keyControl = qobject_cast<CUiAnimViewKeyUIControls*>(pObj);
            Q_ASSERT(keyControl);
            m_keyControls.push_back(keyControl);
        }
    }

    // Sort key controls by descending priority
    std::stable_sort(m_keyControls.begin(), m_keyControls.end(),
        [](const _smart_ptr<CUiAnimViewKeyUIControls>& a, const _smart_ptr<CUiAnimViewKeyUIControls>& b)
        {
            return a->GetPriority() > b->GetPriority();
        }
        );

    CreateAllVars();
#endif
}

//////////////////////////////////////////////////////////////////////////
void CUiAnimViewKeyPropertiesDlg::OnVarChange(IVariable* pVar)
{
}

//////////////////////////////////////////////////////////////////////////
void CUiAnimViewKeyPropertiesDlg::CreateAllVars()
{
    for (int i = 0; i < (int)m_keyControls.size(); i++)
    {
        m_keyControls[i]->SetKeyPropertiesDlg(this);
        m_keyControls[i]->OnCreateVars();
    }
}

//////////////////////////////////////////////////////////////////////////
void CUiAnimViewKeyPropertiesDlg::PopulateVariables()
{
#if UI_ANIMATION_REMOVED    // wndProps
    //SetVarBlock( m_pVarBlock,functor(*this,&CUiAnimViewKeyPropertiesDlg::OnVarChange) );

    // Must first clear any selection in properties window.
    m_wndProps->ClearSelection();
    m_wndProps->RemoveAllItems();
    m_wndProps->AddVarBlock(m_pVarBlock);

    m_wndProps->SetUpdateCallback(functor(*this, &CUiAnimViewKeyPropertiesDlg::OnVarChange));
    //m_wndProps->ExpandAll();


    ReloadValues();
#endif
}

//////////////////////////////////////////////////////////////////////////
void CUiAnimViewKeyPropertiesDlg::PopulateVariables(ReflectedPropertyControl& propCtrl)
{
#if UI_ANIMATION_REMOVED
    propCtrl.ClearSelection();
    propCtrl.RemoveAllItems();
    propCtrl.AddVarBlock(m_pVarBlock);

    propCtrl.ReloadValues();
#endif
}

//////////////////////////////////////////////////////////////////////////
void CUiAnimViewKeyPropertiesDlg::OnKeysChanged(CUiAnimViewSequence* pSequence)
{
    CUiAnimViewKeyBundle selectedKeys = pSequence->GetSelectedKeys();

    if (selectedKeys.GetKeyCount() > 0 && selectedKeys.AreAllKeysOfSameType())
    {
        CUiAnimViewTrack* pTrack = selectedKeys.GetKey(0).GetTrack();

        CUiAnimParamType paramType = pTrack->GetParameterType();
        EUiAnimCurveType trackType = pTrack->GetCurveType();
        EUiAnimValue valueType = pTrack->GetValueType();

        for (int i = 0; i < (int)m_keyControls.size(); i++)
        {
            if (m_keyControls[i]->SupportTrackType(paramType, trackType, valueType))
            {
                m_keyControls[i]->OnKeySelectionChange(selectedKeys);
                break;
            }
        }
    }
}

//////////////////////////////////////////////////////////////////////////
void CUiAnimViewKeyPropertiesDlg::OnKeySelectionChanged(CUiAnimViewSequence* pSequence)
{
    CUiAnimViewKeyBundle selectedKeys = pSequence->GetSelectedKeys();

    m_wndTrackProps->OnKeySelectionChange(selectedKeys);

    bool bSelectChangedInSameTrack
        = m_pLastTrackSelected
            && selectedKeys.GetKeyCount() == 1
            && selectedKeys.GetKey(0).GetTrack() == m_pLastTrackSelected;

    if (selectedKeys.GetKeyCount() == 1)
    {
        m_pLastTrackSelected = selectedKeys.GetKey(0).GetTrack();
    }
    else
    {
        m_pLastTrackSelected = nullptr;
    }

    if (bSelectChangedInSameTrack)
    {
#if UI_ANIMATION_REMOVED
        m_wndProps->ClearSelection();
#endif
    }
    else
    {
        m_pVarBlock->DeleteAllVariables();
    }

#if UI_ANIMATION_REMOVED
    m_wndProps->setEnabled(false);
    bool bAssigned = false;
    if (selectedKeys.GetKeyCount() > 0 && selectedKeys.AreAllKeysOfSameType())
    {
        CUiAnimViewTrack* pTrack = selectedKeys.GetKey(0).GetTrack();

        CUiAnimParamType paramType = pTrack->GetParameterType();
        EUiAnimCurveType trackType = pTrack->GetCurveType();
        EUiAnimValue valueType = pTrack->GetValueType();

        for (int i = 0; i < (int)m_keyControls.size(); i++)
        {
            if (m_keyControls[i]->SupportTrackType(paramType, trackType, valueType))
            {
                if (!bSelectChangedInSameTrack)
                {
                    AddVars(m_keyControls[i]);
                }

                if (m_keyControls[i]->OnKeySelectionChange(selectedKeys))
                {
                    bAssigned = true;
                }

                break;
            }
        }

        m_wndProps->setEnabled(true);
    }
    else
    {
        m_wndProps->setEnabled(false);
    }

    if (bSelectChangedInSameTrack)
    {
        ReloadValues();
    }
    else
    {
        PopulateVariables();
    }
#endif
}

//////////////////////////////////////////////////////////////////////////
void CUiAnimViewKeyPropertiesDlg::AddVars(CUiAnimViewKeyUIControls* pUI)
{
    CVarBlock* pVB = pUI->GetVarBlock();
    for (int i = 0, num = pVB->GetNumVariables(); i < num; i++)
    {
        IVariable* pVar = pVB->GetVariable(i);
        m_pVarBlock->AddVariable(pVar);
    }
}

//////////////////////////////////////////////////////////////////////////
void CUiAnimViewKeyPropertiesDlg::ReloadValues()
{
#if UI_ANIMATION_REMOVED
    if (m_wndProps)
    {
        m_wndProps->ReloadValues();
    }
#endif
}

void CUiAnimViewKeyPropertiesDlg::OnSequenceChanged()
{
    m_wndTrackProps->OnSequenceChanged();
}

//////////////////////////////////////////////////////////////////////////
//////////////////////////////////////////////////////////////////////////
//////////////////////////////////////////////////////////////////////////
CUiAnimViewTrackPropsDlg::CUiAnimViewTrackPropsDlg(QWidget* parent /* = 0 */)
    : QWidget(parent)
    , ui(new Ui::CUiAnimViewTrackPropsDlg)
{
    ui->setupUi(this);
    connect(ui->TIME, SIGNAL(valueChanged(double)), this, SLOT(OnUpdateTime()));
}

CUiAnimViewTrackPropsDlg::~CUiAnimViewTrackPropsDlg()
{
}

//////////////////////////////////////////////////////////////////////////
void CUiAnimViewTrackPropsDlg::OnSequenceChanged()
{
    CUiAnimViewSequence* pSequence = nullptr;
    EBUS_EVENT_RESULT(pSequence, UiEditorAnimationBus, GetCurrentSequence);

    if (pSequence)
    {
        Range range = pSequence->GetTimeRange();
        ui->TIME->setRange(range.start, range.end);
    }
}


//////////////////////////////////////////////////////////////////////////
bool CUiAnimViewTrackPropsDlg::OnKeySelectionChange(CUiAnimViewKeyBundle& selectedKeys)
{
    m_keyHandle = CUiAnimViewKeyHandle();

    if (selectedKeys.GetKeyCount() == 1)
    {
        m_keyHandle = selectedKeys.GetKey(0);
    }

    if (m_keyHandle.IsValid())
    {
        ui->TIME->setValue(m_keyHandle.GetTime());
        ui->PREVNEXT->setText(QString::number(m_keyHandle.GetIndex() + 1));

        ui->PREVNEXT->setEnabled(true);
        ui->TIME->setEnabled(true);
    }
    else
    {
        ui->PREVNEXT->setEnabled(FALSE);
        ui->TIME->setEnabled(FALSE);
    }
    return true;
}

void CUiAnimViewTrackPropsDlg::OnUpdateTime()
{
    if (!m_keyHandle.IsValid())
    {
        return;
    }

    UiAnimUndo undo("Change key time");
    UiAnimUndo::Record(new CUndoTrackObject(m_keyHandle.GetTrack()));

    const float time = (float) ui->TIME->value();
    m_keyHandle.SetTime(time);

    CUiAnimViewKeyHandle newKey = m_keyHandle.GetTrack()->GetKeyByTime(time);

    if (newKey != m_keyHandle)
    {
        SetCurrKey(newKey);
    }
}

void CUiAnimViewTrackPropsDlg::SetCurrKey(CUiAnimViewKeyHandle& keyHandle)
{
    CUiAnimViewSequence* pSequence = nullptr;
    EBUS_EVENT_RESULT(pSequence, UiEditorAnimationBus, GetCurrentSequence);

    if (keyHandle.IsValid())
    {
        UiAnimUndo undo("Select key");
        UiAnimUndo::Record(new CUndoAnimKeySelection(pSequence));

        m_keyHandle.Select(false);
        m_keyHandle = keyHandle;
        m_keyHandle.Select(true);
    }
}

#include <Animation/UiAnimViewKeyPropertiesDlg.moc>