import { useState } from "react";
import { useTranslation } from "react-i18next";
import { Button, Icon, IconSize, useModal } from "@gouvfr-lasuite/ui-components";
import {
  DomainRegistration,
  Organization,
  Service,
} from "@/features/api/Repository";
import { useOperatorContext } from "@/features/layouts/components/GlobalLayout";
import {
  useDomainRegistrations,
  useMutationRefreshDomainRegistration,
} from "@/hooks/useQueries";
import {
  ServiceBlock,
  useServiceBlock,
} from "@/features/ui/components/service/ServiceBlock";
import { ServiceAttribute } from "../ServiceAttribute";
import { ServiceHelpLink } from "../ServiceHelpLink";
import { DomainNameserversModal } from "../DomainNameserversModal";
import { DomainRegistrationModal } from "../DomainRegistrationModal";

const PREFIX = "organizations.services.types.domain_registration";

// Statuses in which the domain can be pointed at other nameservers.
const NAMESERVERS_EDITABLE = ["registered", "active"];
// Statuses worth a warning mark: the domain needs someone's attention.
const NEEDS_ATTENTION = ["blocked", "expired", "failed"];

const formatDate = (value: string | null) =>
  value ? new Date(value).toLocaleDateString("fr-FR") : null;

const sameSet = (a: string[], b: string[]) =>
  a.length === b.length && a.every((name) => b.includes(name));

/** One registered domain: where it stands, and what can be done about it. */
const DomainRegistrationRow = (props: {
  registration: DomainRegistration;
  canEdit: boolean;
  onEditNameservers: () => void;
  onRefresh: () => void;
  isRefreshing: boolean;
}) => {
  const { t } = useTranslation();
  const { registration, canEdit } = props;
  const expires = formatDate(registration.expires_at);
  const delegating =
    NAMESERVERS_EDITABLE.includes(registration.status) &&
    registration.desired_nameservers.length > 0 &&
    !sameSet(registration.desired_nameservers, registration.nameservers);

  return (
    <div className="dc__domain-registration__row">
      <div className="dc__domain-registration__row__header">
        <span className="dc__domain-registration__row__name">
          {registration.domain}
        </span>
        <span
          className={`dc__domain-registration__status dc__domain-registration__status--${registration.status}`}
        >
          {NEEDS_ATTENTION.includes(registration.status) && (
            <Icon name="warning" size={IconSize.SMALL} />
          )}
          {t(`${PREFIX}.status.${registration.status}`)}
        </span>
        {registration.status === "active" && expires && (
          <span className="dc__domain-registration__row__expires">
            {t(`${PREFIX}.expires`, { date: expires })}
          </span>
        )}
        {canEdit && (
          <span className="dc__domain-registration__row__actions">
            {NAMESERVERS_EDITABLE.includes(registration.status) && (
              <Button
                type="button"
                size="small"
                variant="secondary"
                onClick={props.onEditNameservers}
              >
                {t(`${PREFIX}.nameservers`)}
              </Button>
            )}
            {registration.status !== "failed" &&
              registration.status !== "released" && (
                <Button
                  type="button"
                  size="small"
                  variant="secondary"
                  icon={<Icon name="refresh" />}
                  title={t(`${PREFIX}.refresh`)}
                  disabled={props.isRefreshing}
                  onClick={props.onRefresh}
                />
              )}
          </span>
        )}
      </div>
      <div className="dc__domain-registration__row__details">
        {t(`${PREFIX}.hints.${registration.status}`, { defaultValue: "" })}
        {registration.last_error && registration.status === "requested" && (
          <> {t(`${PREFIX}.hints.retrying`)}</>
        )}
        {delegating && <> {t(`${PREFIX}.hints.delegating`)}</>}
        {registration.registrar_message && (
          <>
            {" "}
            <i>{registration.registrar_message}</i>
          </>
        )}
      </div>
    </div>
  );
};

/**
 * The domain registration service block: the domain names registered for the
 * organization through the operator's registrar account, and where each stands.
 * A registration takes anywhere from a minute to several days (some names are
 * examined by the registry first): the list refreshes while any is in progress.
 */
export const DomainRegistrationServiceBlock = (props: {
  service: Service;
  organization: Organization;
}) => {
  const { t } = useTranslation();
  const { operatorId } = useOperatorContext();
  const blockProps = useServiceBlock(props.service, props.organization);
  const registerModal = useModal();
  const [editing, setEditing] = useState<DomainRegistration | null>(null);

  const isActive = !!props.service.subscription?.is_active;
  const registrations = useDomainRegistrations(
    operatorId,
    props.organization.id,
    isActive
  );
  const refresh = useMutationRefreshDomainRegistration(
    operatorId,
    props.organization.id
  );
  const canEdit = isActive && !blockProps.isManagedByOtherOperator;
  const items = registrations.data ?? [];

  return (
    <ServiceBlock
      {...blockProps}
      content={
        <div className="dc__service__attribute__container">
          {registerModal.isOpen && (
            <DomainRegistrationModal
              {...registerModal}
              organizationId={props.organization.id}
              instanceName={props.service.instance_name}
            />
          )}
          {editing && (
            <DomainNameserversModal
              isOpen
              onClose={() => setEditing(null)}
              organizationId={props.organization.id}
              registration={editing}
            />
          )}
          <ServiceAttribute name={t(`${PREFIX}.domains.label`)}>
            {isActive && registrations.isError && (
              <div className="dc__service__warning">
                <Icon name="warning" size={IconSize.SMALL} />
                {t(`${PREFIX}.load_failed`)}
              </div>
            )}
            {isActive && items.length > 0 && (
              <div className="dc__domain-registration__list">
                {items.map((registration) => (
                  <DomainRegistrationRow
                    key={registration.id}
                    registration={registration}
                    canEdit={canEdit && registration.editable}
                    onEditNameservers={() => setEditing(registration)}
                    onRefresh={() => refresh.mutate(registration.id)}
                    isRefreshing={refresh.isPending}
                  />
                ))}
              </div>
            )}
            {(!isActive || (items.length === 0 && !registrations.isError)) && (
              <div className="dc__domain-registration__empty">
                {t(`${PREFIX}.domains.empty`)}
              </div>
            )}
          </ServiceAttribute>
          {canEdit && (
            <Button
              type="button"
              variant="secondary"
              size="small"
              icon={<Icon name="add" />}
              onClick={() => registerModal.open()}
            >
              {t(`${PREFIX}.register`)}
            </Button>
          )}
          <ServiceHelpLink url={props.service.config?.help_center_url} />
        </div>
      }
    />
  );
};
